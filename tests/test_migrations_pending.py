import os

import pytest


@pytest.mark.postgres
def test_neon_migration_requires_external_database_url():
    if not os.environ.get("NEON_TEST_DATABASE_URL"):
        pytest.skip("pending external Neon development URL")

