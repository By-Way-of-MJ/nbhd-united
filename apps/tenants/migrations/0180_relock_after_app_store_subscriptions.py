"""Relock public tables after billing 0012 (App Store subscriptions + notification
ledger). Enables RLS on any owned public table missing it (no policies — see
``test_no_policies_on_public_schema``) and, as for ``credit_ledger`` (0085), revokes
the PostgREST API roles' grants on the two new money tables. Guarded no-ops where the
roles don't exist (CI/local)."""

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

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON public.app_store_subscriptions FROM anon';
    EXECUTE 'REVOKE ALL ON public.app_store_notifications FROM anon';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    EXECUTE 'REVOKE ALL ON public.app_store_subscriptions FROM authenticated';
    EXECUTE 'REVOKE ALL ON public.app_store_notifications FROM authenticated';
  END IF;
END
$$;
"""

REVERSE_SQL = "-- Reversing would leave the App Store money tables unlocked. Do not auto-reverse."


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0012_app_store_subscriptions"),
        ("tenants", "0179_tenant_stripe_subscription_ended_at"),
    ]

    operations = [migrations.RunSQL(RELOCK_SQL, REVERSE_SQL)]
