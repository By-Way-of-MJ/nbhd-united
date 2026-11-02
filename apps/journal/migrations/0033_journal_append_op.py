"""Daily append receipts with a transaction-local tenant RLS backstop."""

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0032_alter_document_kind"),
        ("tenants", "0190_relock_after_keep_chatting"),
    ]

    operations = [
        migrations.CreateModel(
            name="JournalAppendOp",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("op_id", models.UUIDField()),
                ("document_slug", models.CharField(max_length=128)),
                ("entry_heading", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="journal_append_ops",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "journal_append_ops",
                "constraints": [
                    models.UniqueConstraint(fields=("tenant", "op_id"), name="journal_append_op_tenant_op_unique")
                ],
            },
        ),
        # Named-role FORCE RLS, as in transcripts.0001. StepPrep's bare
        # relock alone would not provide a tenant backstop for this table.
        migrations.RunSQL(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                    CREATE ROLE app_user NOLOGIN;
                END IF;
            END
            $$;
            ALTER TABLE journal_append_ops ENABLE ROW LEVEL SECURITY;
            ALTER TABLE journal_append_ops FORCE ROW LEVEL SECURITY;
            CREATE POLICY journal_append_ops_tenant ON journal_append_ops TO app_user
            USING (
                tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
                OR coalesce(current_setting('app.service_role', true), '') = 'true'
            )
            WITH CHECK (
                tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
                OR coalesce(current_setting('app.service_role', true), '') = 'true'
            );
            GRANT SELECT, INSERT, UPDATE, DELETE ON journal_append_ops TO app_user;
            """,
            """
            DROP POLICY IF EXISTS journal_append_ops_tenant ON journal_append_ops;
            ALTER TABLE journal_append_ops NO FORCE ROW LEVEL SECURITY;
            """,
        ),
    ]
