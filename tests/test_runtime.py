import sys
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from compliance_service.contracts import InvalidRequest
from compliance_service.runtime import build_service, connection_factory, load_production_settings


CONFIG = """
[service]
source_system = "oceanbase"
environment = "test"
check_url = "/inspection/tasks"
[oceanbase]
compatibility_mode = "mysql"
driver_module = "nonexistent_driver_for_test"
host_env = "OB_HOST"
port_env = "OB_PORT"
database_env = "OB_DB"
user_env = "OB_USER"
password_env = "OB_PASS"
[callback]
allowed_hosts = ["callback.test"]
[attachments]
allowed_hosts = ["files.test"]
[[principals]]
token_env = "TASK_TOKEN"
allowed_mines = ["001234"]
[status_map]
"""


class RuntimeTests(unittest.TestCase):
    def write_config(self, root, content=CONFIG):
        path = Path(root) / "production.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def env(self):
        return {"OB_HOST": "db.test", "OB_PORT": "2883", "OB_DB": "sy_xian_mine_dev",
                "OB_USER": "colliery@co", "OB_PASS": "secret-never-logged", "TASK_TOKEN": "x" * 32}

    def test_config_load_rejects_missing_required_sections(self):
        with TemporaryDirectory() as td:
            path = self.write_config(td, "[service]\n")
            with self.assertRaises(InvalidRequest):
                load_production_settings(path)

    def test_build_is_lazy_and_does_not_import_driver_or_connect(self):
        with TemporaryDirectory() as td:
            path = self.write_config(td)
            service = build_service(Path(td) / "service.sqlite3", path, env=self.env())
            self.assertEqual("oceanbase", service.source_system)
            self.assertEqual({"001234"}, service.allowed_mines)
            self.assertEqual("x" * 32, service.task_token)
            self.assertEqual("/inspection/tasks", service.check_url)
            # The configured module is intentionally missing; lazy construction
            # proves build_service did not connect or import it.
            self.assertIsNotNone(service.repository)

    def test_secrets_are_not_in_repr_or_config_return(self):
        with TemporaryDirectory() as td:
            path = self.write_config(td)
            service = build_service(Path(td) / "service.sqlite3", path, env=self.env())
            settings = load_production_settings(path)
            self.assertNotIn("secret-never-logged", repr(settings))
            self.assertNotIn("secret-never-logged", repr(service.repository))

    def test_invalid_route_and_network_allowlist_rejected(self):
        with TemporaryDirectory() as td:
            for bad in (
                CONFIG.replace('check_url = "/inspection/tasks"', 'check_url = "https://bad"'),
                CONFIG.replace('check_url = "/inspection/tasks"', 'check_url = "/v1/custom"'),
                CONFIG.replace('allowed_hosts = ["callback.test"]', 'allowed_hosts = []'),
                CONFIG.replace('compatibility_mode = "mysql"', 'compatibility_mode = "oracle"'),
            ):
                with self.subTest(configuration=bad):
                    path = self.write_config(td, bad)
                    with self.assertRaises(InvalidRequest):
                        build_service(Path(td) / "service.sqlite3", path, env=self.env())

    def test_connection_factory_defers_missing_driver_until_worker(self):
        with TemporaryDirectory() as td:
            path = self.write_config(td)
            service = build_service(Path(td) / "service.sqlite3", path, env=self.env())
            with self.assertRaises(InvalidRequest):
                service.repository.connection_factory()

    def test_check_url_rejects_all_reserved_routes_and_prefixes(self):
        with TemporaryDirectory() as td:
            for route in ("/v1/manual/runs", "/health", "/debug", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
                with self.subTest(route=route):
                    path = self.write_config(td, CONFIG.replace('check_url = "/inspection/tasks"', f'check_url = "{route}"'))
                    with self.assertRaises(InvalidRequest):
                        build_service(Path(td) / "service.sqlite3", path, env=self.env())

    def test_connection_resolves_environment_values_only_on_call(self):
        with TemporaryDirectory() as td:
            path = self.write_config(td)
            settings = load_production_settings(path)["oceanbase"]
            calls = []
            fake = SimpleNamespace(connect=lambda **kwargs: calls.append(kwargs) or "connection")
            environment = self.env()
            factory = connection_factory(settings, env=environment)
            self.assertEqual([], calls)
            with patch.dict(sys.modules, {"nonexistent_driver_for_test": fake}):
                self.assertEqual("connection", factory())
            self.assertEqual(2883, calls[0]["port"])
            self.assertEqual("secret-never-logged", calls[0]["password"])
            self.assertEqual("sy_xian_mine_dev", calls[0]["database"])

    def test_connection_error_never_exposes_driver_secret_message(self):
        with TemporaryDirectory() as td:
            settings = load_production_settings(self.write_config(td))["oceanbase"]
            def fail(**kwargs):
                raise RuntimeError(f"password={kwargs['password']}")
            with patch.dict(sys.modules, {"nonexistent_driver_for_test": SimpleNamespace(connect=fail)}):
                with self.assertRaises(RuntimeError) as caught:
                    connection_factory(settings, env=self.env())()
            self.assertNotIn("secret-never-logged", str(caught.exception))

    def test_task_authentication_must_be_configured(self):
        with TemporaryDirectory() as td:
            environment = self.env()
            del environment["TASK_TOKEN"]
            with self.assertRaises(InvalidRequest):
                build_service(Path(td) / "service.sqlite3", self.write_config(td), env=environment)

    def test_status_map_is_opt_in(self):
        with TemporaryDirectory() as td:
            path = self.write_config(td, CONFIG + 'paused = "pause"\nresumed = "resume"\n')
            service = build_service(Path(td) / "service.sqlite3", path, env=self.env())
            self.assertEqual({"paused": "pause", "resumed": "resume"}, service.status_map)

    def local_debug_config(self):
        return (CONFIG.replace('environment = "test"', 'environment = "local"\nlocal_debug = true')
                .replace('[callback]\nallowed_hosts = ["callback.test"]\n', '')
                .replace('token_env = "TASK_TOKEN"\n', ''))

    def test_local_debug_needs_no_callback_or_task_token(self):
        with TemporaryDirectory() as td:
            environment = self.env()
            environment.pop("TASK_TOKEN")
            service = build_service(Path(td) / "local.sqlite3", self.write_config(td, self.local_debug_config()), env=environment)
            self.assertTrue(service.local_debug)
            self.assertEqual({"001234"}, service.allowed_mines)
            self.assertIsNone(service.task_token)
            self.assertIsNone(service.callback_client)
            self.assertIsNotNone(service.repository)
            self.assertIsNotNone(service.acquisition)

    def test_local_debug_ignores_callback_secrets_and_cannot_enable_production(self):
        with TemporaryDirectory() as td:
            for profile in ("local", "test"):
                configuration = CONFIG.replace('environment = "test"', f'environment = "{profile}"\nlocal_debug = true')
                service = build_service(Path(td) / "local.sqlite3", self.write_config(td, configuration), env={})
                self.assertIsNone(service.task_token)
                self.assertIsNone(service.callback_client)
            invalid = self.local_debug_config().replace('environment = "local"', 'environment = "production"')
            with self.assertRaises(InvalidRequest):
                build_service(Path(td) / "bad.sqlite3", self.write_config(td, invalid), env={})

    def test_local_debug_flag_must_be_boolean(self):
        with TemporaryDirectory() as td:
            invalid = self.local_debug_config().replace('local_debug = true', 'local_debug = "true"')
            with self.assertRaises(InvalidRequest):
                build_service(Path(td) / "bad.sqlite3", self.write_config(td, invalid), env={})

    def test_local_debug_cli_rejects_external_bind(self):
        from compliance_service.__main__ import main
        with patch("compliance_service.runtime.build_service", return_value=SimpleNamespace(local_debug=True)), \
             patch.object(sys, "argv", ["compliance-service", "serve", "--host", "0.0.0.0"]), \
             patch("uvicorn.run") as run:
            with self.assertRaisesRegex(SystemExit, "loopback"):
                main()
            run.assert_not_called()

    def test_unconfigured_local_cli_rejects_external_bind(self):
        from compliance_service.__main__ import main
        with patch("compliance_service.runtime.build_service", return_value=SimpleNamespace(task_token=None)), \
             patch.object(sys, "argv", ["compliance-service", "serve", "--host", "0.0.0.0"]), \
             patch("uvicorn.run") as run:
            with self.assertRaisesRegex(SystemExit, "loopback"):
                main()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
