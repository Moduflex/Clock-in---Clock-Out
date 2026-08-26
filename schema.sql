-- Schema for the face-recognition clocking system (MySQL 8, InnoDB, utf8mb4).
-- Generated from app/models.py. The application creates these tables itself
-- via scripts/init_db.py; this file is for DBAs and for review.

SET NAMES utf8mb4;

CREATE TABLE admin_user (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	username VARCHAR(64) NOT NULL, 
	password_hash VARCHAR(128) NOT NULL, 
	full_name VARCHAR(128), 
	is_active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_login_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_admin_username UNIQUE (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE working_week (
	id INTEGER NOT NULL AUTO_INCREMENT,
	name VARCHAR(64) NOT NULL,
	hours FLOAT NOT NULL,
	is_default BOOL NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Contracted hours per week. Paid hours beyond this in a Monday-Sunday week are
-- reported as overtime; see app/services/timesheet.py.

CREATE TABLE shift_pattern (
	id INTEGER NOT NULL AUTO_INCREMENT,
	name VARCHAR(64) NOT NULL,
	start_time TIME NOT NULL,
	end_time TIME NOT NULL,
	unpaid_break_minutes INTEGER NOT NULL,
	break_applies_after_minutes INTEGER NOT NULL DEFAULT 360,
	pay_beyond_end BOOL NOT NULL DEFAULT 1,
	is_default BOOL NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE employee (
	id INTEGER NOT NULL AUTO_INCREMENT,
	payroll_ref VARCHAR(32) NOT NULL,
	first_name VARCHAR(64) NOT NULL,
	last_name VARCHAR(64) NOT NULL,
	department VARCHAR(64),
	email VARCHAR(190),
	is_active BOOL NOT NULL,
	shift_pattern_id INTEGER,
	working_week_id INTEGER,
	-- The basic hourly rate, encrypted by the application (Fernet); never a
	-- plain number. The key lives in .env as PAYROLL_KEY, so a database dump
	-- reveals no wages. There is no overtime column: that rate is basic * 1.5,
	-- worked out on demand so the two can never disagree.
	basic_rate_enc BLOB(256),
	-- 'four_weekly' or 'salary'. Four-weekly staff are paid from clocked hours
	-- and appear on the payroll master sheet; salaried staff are paid a fixed
	-- amount whatever they clock, so they are left off it.
	pay_basis VARCHAR(16) NOT NULL DEFAULT 'four_weekly',
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (payroll_ref),
	FOREIGN KEY(shift_pattern_id) REFERENCES shift_pattern (id) ON DELETE SET NULL,
	FOREIGN KEY(working_week_id) REFERENCES working_week (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Upgrading a database created before the shifts feature:
--   ALTER TABLE employee ADD COLUMN shift_pattern_id INTEGER;
--   ALTER TABLE shift_pattern ADD COLUMN break_applies_after_minutes INTEGER NOT NULL DEFAULT 360;
-- Upgrading a database created before pay rates:
--   ALTER TABLE employee ADD COLUMN basic_rate_enc BLOB;
-- Upgrading a database created before the salary/four-weekly split:
--   ALTER TABLE employee ADD COLUMN pay_basis VARCHAR(16) NOT NULL
--     DEFAULT 'four_weekly';
-- An overtime_rate_enc column from an earlier build is no longer read; the
-- overtime rate is derived. Drop it once you are happy nothing needs it:
--   ALTER TABLE employee DROP COLUMN overtime_rate_enc;
-- Upgrading a database created before overtime:
--   (create working_week above, then)
--   ALTER TABLE employee ADD COLUMN working_week_id INTEGER;
--   ALTER TABLE shift_pattern ADD COLUMN pay_beyond_end BOOLEAN NOT NULL DEFAULT 1;
-- (scripts/init_db.py does all of this automatically when re-run.)

CREATE TABLE attendance_event (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	employee_id INTEGER NOT NULL, 
	direction VARCHAR(8) NOT NULL, 
	occurred_at DATETIME NOT NULL, 
	method VARCHAR(16) NOT NULL, 
	confidence FLOAT, 
	device_label VARCHAR(64), 
	note TEXT, 
	is_voided BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	created_by_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(employee_id) REFERENCES employee (id) ON DELETE CASCADE, 
	FOREIGN KEY(created_by_id) REFERENCES admin_user (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_attendance_employee_time ON attendance_event (employee_id, occurred_at);
CREATE INDEX ix_attendance_event_occurred_at ON attendance_event (occurred_at);

CREATE TABLE fingerprint_credential (
	id INTEGER NOT NULL AUTO_INCREMENT,
	employee_id INTEGER NOT NULL,
	device_label VARCHAR(64) NOT NULL,
	finger_id INTEGER NOT NULL,
	label VARCHAR(64),
	is_active BOOL NOT NULL,
	created_at DATETIME NOT NULL,
	created_by_id INTEGER,
	last_used_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_fingerprint_device_slot UNIQUE (device_label, finger_id),
	FOREIGN KEY(employee_id) REFERENCES employee (id) ON DELETE CASCADE,
	FOREIGN KEY(created_by_id) REFERENCES admin_user (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Holds no biometric data: only which slot on which reader belongs to whom.
CREATE INDEX ix_fingerprint_credential_employee_id ON fingerprint_credential (employee_id);
CREATE INDEX ix_fingerprint_lookup ON fingerprint_credential (device_label, finger_id);

CREATE TABLE fingerprint_template (
	id INTEGER NOT NULL AUTO_INCREMENT,
	employee_id INTEGER NOT NULL,
	template BLOB(4096) NOT NULL,
	driver VARCHAR(32) NOT NULL,
	position INTEGER,
	quality FLOAT,
	created_at DATETIME NOT NULL,
	created_by_id INTEGER,
	PRIMARY KEY (id),
	FOREIGN KEY(employee_id) REFERENCES employee (id) ON DELETE CASCADE,
	FOREIGN KEY(created_by_id) REFERENCES admin_user (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Real biometric data, unlike fingerprint_credential. Only used with readers
-- that hand back a template for the application to match.
CREATE INDEX ix_fingerprint_template_employee_id ON fingerprint_template (employee_id);
CREATE INDEX ix_fingerprint_template_lookup ON fingerprint_template (driver, employee_id);

CREATE TABLE face_template (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	employee_id INTEGER NOT NULL, 
	embedding BLOB(2048) NOT NULL, 
	dimensions INTEGER NOT NULL, 
	sharpness FLOAT, 
	face_pixels INTEGER, 
	created_at DATETIME NOT NULL, 
	created_by_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(employee_id) REFERENCES employee (id) ON DELETE CASCADE, 
	FOREIGN KEY(created_by_id) REFERENCES admin_user (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_face_template_employee_id ON face_template (employee_id);
