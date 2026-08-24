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

CREATE TABLE shift_pattern (
	id INTEGER NOT NULL AUTO_INCREMENT,
	name VARCHAR(64) NOT NULL,
	start_time TIME NOT NULL,
	end_time TIME NOT NULL,
	unpaid_break_minutes INTEGER NOT NULL,
	break_applies_after_minutes INTEGER NOT NULL DEFAULT 360,
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
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (payroll_ref),
	FOREIGN KEY(shift_pattern_id) REFERENCES shift_pattern (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Upgrading a database created before the shifts feature:
--   ALTER TABLE employee ADD COLUMN shift_pattern_id INTEGER;
--   ALTER TABLE shift_pattern ADD COLUMN break_applies_after_minutes INTEGER NOT NULL DEFAULT 360;
-- (scripts/init_db.py does this automatically when re-run.)

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
