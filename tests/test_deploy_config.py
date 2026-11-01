"""Deploy files must not fight the app's own migration runner or lose the site."""
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def test_prod_postgres_does_not_preapply_migrations():
    # Mounting migrations into docker-entrypoint-initdb.d applies them without recording them in
    # schema_migrations, so the app's runner would apply them again and crash on the first start.
    assert "docker-entrypoint-initdb.d" not in (DEPLOY / "docker-compose.prod.yml").read_text()


def test_caddy_serves_the_site_for_non_api_paths():
    caddy = (DEPLOY / "Caddyfile").read_text()
    assert "file_server" in caddy and "/srv/site" in caddy
    assert "/srv/site" in (DEPLOY / "docker-compose.prod.yml").read_text()


def test_caddy_proxies_ops_metrics_and_model_info_routes():
    caddy = (DEPLOY / "Caddyfile").read_text()
    matcher = next(line for line in caddy.splitlines() if "@app path" in line)
    assert "/metrics" in matcher and "/model-info" in matcher
