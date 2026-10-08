# Relock public tables after friends 0018 (neighbor_photos).

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
        ("friends", "0018_profile_photos"),
        ("tenants", "0175_tenant_image_refresh_blocked_tag"),
    ]

    operations = [migrations.RunSQL(RELOCK_SQL, REVERSE_SQL)]
