"""Identity-backed family records, reviewed drafts and media delivery."""

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade():
    op.add_column("discussions", sa.Column("attachments_json", sa.Text))
    op.add_column("outbox", sa.Column("media_json", sa.Text))
    op.create_table(
        "family_members",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("participant_id", sa.String, unique=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("created_by", sa.String, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
    )
    op.create_table(
        "member_aliases",
        sa.Column("owner", sa.String, primary_key=True),
        sa.Column("alias", sa.String, primary_key=True),
        sa.Column("member_id", sa.String, sa.ForeignKey("family_members.id"), nullable=False),
    )
    op.create_table(
        "confirmations",
        sa.Column("code", sa.String, primary_key=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("sender", sa.String, nullable=False),
        sa.Column("confirmer", sa.String, nullable=False),
        sa.Column("message_id", sa.String, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("expires_at", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("decided_at", sa.Integer),
    )
    op.create_table(
        "plan_comparisons",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("discussion_id", sa.String, sa.ForeignKey("discussions.id")),
        sa.Column("sender", sa.String, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
    )
    op.create_table(
        "imported_itinerary",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "confirmation_code", sa.String, sa.ForeignKey("confirmations.code"), nullable=False
        ),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("start", sa.String),
        sa.Column("end", sa.String),
        sa.Column("location", sa.String),
        sa.Column("reference", sa.String),
        sa.Column("reported_by", sa.String, nullable=False),
        sa.Column("source_message_id", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
    )

    conn = op.get_bind()
    old = (
        conn.execute(sa.text("SELECT DISTINCT subject,author FROM family_preferences"))
        .mappings()
        .all()
    )
    for pref in old:
        label = "reported:" + pref["author"] + ":" + pref["subject"].casefold()
        identity = "member_" + hashlib.sha256(label.encode()).hexdigest()[:20]
        conn.execute(
            sa.text(
                "INSERT OR IGNORE INTO family_members(id,name,created_by,created_at) "
                "VALUES (:id,:name,:author,0)"
            ),
            {"id": identity, "name": pref["subject"], "author": pref["author"]},
        )
        conn.execute(
            sa.text(
                "INSERT OR IGNORE INTO member_aliases(owner,alias,member_id) "
                "VALUES (:owner,:alias,:id)"
            ),
            {"owner": pref["author"], "alias": pref["subject"].casefold(), "id": identity},
        )
        conn.execute(
            sa.text(
                "UPDATE family_preferences SET subject=:id WHERE subject=:name AND author=:author"
            ),
            {"id": identity, "name": pref["subject"], "author": pref["author"]},
        )


def downgrade():
    raise RuntimeError("Review identity and imported-data retention before downgrading")
