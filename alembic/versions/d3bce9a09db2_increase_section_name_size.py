"""increase section name size

Revision ID: d3bce9a09db2
Revises: 87229110fc79
Create Date: 2023-09-15 10:50:04.549656

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'd3bce9a09db2'
down_revision = '87229110fc79'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('vbpl_toan_van', 'section_name',
               existing_type=mysql.VARCHAR(collation='utf8mb4_unicode_ci', length=200),
               type_=sa.String(length=400),
               existing_nullable=True)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('vbpl_toan_van', 'section_name',
               existing_type=sa.String(length=400),
               type_=mysql.VARCHAR(collation='utf8mb4_unicode_ci', length=200),
               existing_nullable=True)
    # ### end Alembic commands ###
