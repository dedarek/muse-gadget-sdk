import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "gateway-firmware/main/gateway_main.c").read_text()


class EnterpriseGuardTests(unittest.TestCase):
    def test_credentials_have_no_flash_write_or_reboot(self):
        body = SOURCE.split("static void enterprise_configure", 1)[1].split("static void gateway_session", 1)[0]
        self.assertNotIn("nvs_", body)
        self.assertNotIn("esp_restart", body)
        self.assertIn("esp_eap_client_set_password", body)

    def test_server_validation_precedes_password_and_is_not_disabled(self):
        body = SOURCE.split("static void enterprise_configure", 1)[1].split("static void gateway_session", 1)[0]
        self.assertLess(body.index("esp_eap_client_set_ca_cert"), body.index("esp_eap_client_set_password"))
        self.assertLess(body.index("esp_eap_client_set_domain_name"), body.index("esp_eap_client_set_password"))
        self.assertIn("esp_eap_client_set_disable_time_check(false)", body)
        self.assertNotIn("esp_eap_client_set_disable_time_check(true)", body)
        self.assertIn("!atomic_load(&s_enterprise_active) && s_ssid[0]", SOURCE)

    def test_gateway_session_requires_authenticated_network_and_is_ram_only(self):
        body = SOURCE.split("static void gateway_session", 1)[1].split("static void configure", 1)[0]
        self.assertIn("atomic_load(&s_enterprise_active) && atomic_load(&s_wifi)", body)
        self.assertNotIn("nvs_", body)
        self.assertNotIn("esp_restart", body)


if __name__ == "__main__":
    unittest.main()
