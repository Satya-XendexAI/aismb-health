"""
config.py — Where the database's "address and key" are kept.

In simple terms: this file just reads the settings needed to connect to
the database (like its address, name, and password) from a separate,
private settings file (.env), instead of typing them directly into the
code. That way the password never has to be visible in the code itself.
"""

import os
from dotenv import load_dotenv

# Load .env file from the same directory as this script
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "hospital_dev")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
