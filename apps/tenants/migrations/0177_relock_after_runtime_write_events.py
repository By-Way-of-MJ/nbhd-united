# Relock public tables after router 0036 (runtime_write_events).

from django.db import migrations

RELOCK_SQL = r"""
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT schemaname, tablename
    FROM pg_tables
    WHERE schemaname = 'public'
      AND tableowner = current_user
      AND rowsecurity = false
  LOOP
    EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
                   r.schemaname, r.tablename);
  END LOOP;
END
$$;
"""

REVERSE_SQL = "-- Reversing would leave public tables unlocked at migration time. Do not auto-reverse."


class Migration(migrations.Migration):
    dependencies = [
        ("router", "0036_runtime_write_events"),
        ("tenants", "0176_relock_after_profile_photos"),
    ]

    operations = [migrations.RunSQL(RELOCK_SQL, REVERSE_SQL)]
