import json
import socket
import tempfile
import unittest
from pathlib import Path

from napcat_adapter.auto_config import SERVICE_NAME, configure_local, find_configs, saved_token, server_url


class AutoConfigTests(unittest.TestCase):
    def test_owned_service_upgrades_self_reporting_without_changing_token_or_port(self):
        service = dict(name=SERVICE_NAME, enable=True, host="127.0.0.1", port=32001,
                       messagePostFormat="array", reportSelfMessage=False, token="test-owned-token",
                       enableForcePushEvent=True, debug=False, heartInterval=30000)
        original = self.write(dict(network=dict(websocketServers=[service])))
        result = configure_local(self.path)
        self.assertTrue(result.changed)
        self.assertEqual(result.backup.read_bytes(), original)
        updated = json.loads(self.path.read_text(encoding="utf-8"))["network"]["websocketServers"][0]
        self.assertTrue(updated["reportSelfMessage"])
        self.assertEqual(updated["token"], service["token"])
        self.assertEqual(updated["port"], service["port"])
        self.assertFalse(configure_local(self.path).changed)

    def test_incomplete_external_service_is_preserved_and_dedicated_service_is_added(self):
        for overrides in (dict(reportSelfMessage=False), dict(enableForcePushEvent=False)):
            service = dict(name="another-app", enable=True, host="127.0.0.1", port=3001,
                           messagePostFormat="array", reportSelfMessage=True, enableForcePushEvent=True,
                           token="test-other-token")
            service.update(overrides)
            self.write(dict(network=dict(websocketServers=[service])))
            result = configure_local(self.path)
            updated = json.loads(self.path.read_text(encoding="utf-8"))["network"]["websocketServers"]
            self.assertTrue(result.changed)
            self.assertEqual(updated[0], service)
            self.assertEqual(len(updated), 2)
            self.assertEqual(updated[1]["name"], SERVICE_NAME)
            self.assertTrue(updated[1]["reportSelfMessage"])
            self.assertTrue(updated[1]["enableForcePushEvent"])
            self.assertNotEqual(updated[1]["port"], service["port"])

    def test_localhost_is_not_forced_to_ipv4_and_saved_token_matches(self):
        service = dict(name="existing", enable=True, host="localhost", port=32001,
                       messagePostFormat="array", reportSelfMessage=True, token="test-local-token")
        self.write(dict(network=dict(websocketServers=[service])))
        self.assertEqual(server_url(service), "ws://localhost:32001")
        result = configure_local(self.path)
        self.assertFalse(result.changed)
        self.assertEqual(result.url, "ws://localhost:32001")
        self.assertEqual(saved_token(str(self.path), result.url), service["token"])
        self.assertEqual(server_url(dict(host="::1", port=32001)), "ws://[::1]:32001")

    def test_default_template_cannot_report_success_when_account_files_override_it(self):
        self.write(dict(network=dict(websocketServers=[])))
        template = self.directory / "onebot11.json"
        original = '{"network":{"websocketServers":[]}}'
        template.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "账号.*配置"):
            configure_local(template)
        self.assertEqual(template.read_text(encoding="utf-8"), original)
        self.assertEqual(find_configs(str(template), roots=[self.directory]), [self.path.resolve()])
        self.assertFalse(list(self.directory.glob("*.bak")))

    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".artifacts"
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "onebot11_12345.json"

    def write(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.path.read_bytes()

    def test_add_service_backup_preserve_other_settings_and_idempotent(self):
        data = dict(network=dict(httpServers=[dict(name="existing", enable=True, port=3001)],
                                 websocketClients=[dict(url="ws://example.invalid")],
                                 websocketServers=[]), custom=dict(keep="中文"))
        original = self.write(data)
        result = configure_local(self.path)
        self.assertTrue(result.changed)
        self.assertEqual(result.backup.read_bytes(), original)
        updated = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(updated["custom"], data["custom"])
        self.assertEqual(updated["network"]["httpServers"], data["network"]["httpServers"])
        self.assertEqual(updated["network"]["websocketClients"], data["network"]["websocketClients"])
        service = updated["network"]["websocketServers"][0]
        self.assertEqual(service["name"], SERVICE_NAME)
        self.assertEqual(service["host"], "127.0.0.1")
        self.assertNotEqual(service["port"], 3001)
        self.assertEqual(service["messagePostFormat"], "array")
        self.assertGreaterEqual(len(result.token), 32)
        self.assertNotIn(result.token, repr(result))
        again = configure_local(self.path)
        self.assertFalse(again.changed)
        self.assertEqual(again.token, result.token)
        self.assertEqual(again.url, result.url)
        self.assertEqual(len(list(self.directory.glob("*.bak"))), 1)
        self.assertEqual(saved_token(str(self.path), result.url), result.token)
        self.assertEqual(saved_token(str(self.path), "ws://127.0.0.1:9999"), "")

    def test_reuse_existing_service_without_writing(self):
        service = dict(name="existing", enable=True, host="0.0.0.0", port=32000,
                       messagePostFormat="array", reportSelfMessage=True, token="existing-secret")
        raw = self.write(dict(network=dict(websocketServers=[service])))
        result = configure_local(self.path)
        self.assertFalse(result.changed)
        self.assertIsNone(result.backup)
        self.assertEqual(result.url, "ws://127.0.0.1:32000")
        self.assertEqual(result.token, "existing-secret")
        self.assertEqual(self.path.read_bytes(), raw)

    def test_string_service_untouched_and_new_port_avoids_collisions(self):
        old = dict(name="another-bot", enable=True, host="127.0.0.1", port=3001,
                   messagePostFormat="string", token="other")
        self.write(dict(network=dict(websocketServers=[old])))
        result = configure_local(self.path)
        servers = json.loads(self.path.read_text(encoding="utf-8"))["network"]["websocketServers"]
        self.assertEqual(servers[0], old)
        self.assertNotEqual(servers[1]["port"], 3001)
        self.assertTrue(result.changed)

    def test_invalid_file_never_changes_original(self):
        for data in ({"model": "not-napcat"}, {"network": {"websocketServers": {}}},
                     {"network": {"websocketServers": [None]}}):
            raw = self.write(data)
            with self.assertRaises(ValueError):
                configure_local(self.path)
            self.assertEqual(self.path.read_bytes(), raw)
        self.path.write_text("{ not JSON", encoding="utf-8")
        with self.assertRaises(ValueError):
            configure_local(self.path)
        self.assertFalse(list(self.directory.glob("*.bak")))

    def test_accounts_are_not_silently_selected_and_saved_file_is_preferred(self):
        default = self.directory / "onebot11.json"
        default.write_text("{}", encoding="utf-8")
        self.write(dict(network={}))
        another = self.directory / "onebot11_67890.json"
        another.write_text("{}", encoding="utf-8")
        found = find_configs(roots=[self.directory])
        self.assertEqual(set(found), {self.path.resolve(), another.resolve()})
        self.assertEqual(find_configs(str(self.path), roots=[self.directory]), [self.path.resolve()])

    def test_new_service_avoids_port_occupied_by_another_process(self):
        self.write(dict(network=dict(websocketServers=[])))
        with socket.socket() as occupied:
            try:
                occupied.bind(("127.0.0.1", 3001))
            except OSError:
                # 已有本机服务占用时，同样要求避开该端口，不连接或修改现有服务。
                pass
            result = configure_local(self.path)
        port = int(result.url.rsplit(":", 1)[1])
        self.assertNotEqual(port, 3001)
