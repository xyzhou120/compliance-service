from hashlib import sha256
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, HTTPSHandler

from compliance_service.acquisition import _PinnedConnection, _PinnedHTTPSHandler
from compliance_service.contracts import InvalidRequest


class AttachmentTLSTests(unittest.TestCase):
    def test_pin_is_checked_during_connect_before_any_http_request(self):
        certificate = b"certificate-bytes"
        connection = _PinnedConnection("files.test", certificate_sha256=sha256(certificate).hexdigest())
        def connected(_):
            connection.sock = Mock()
            connection.sock.getpeercert.return_value = certificate
        with patch("http.client.HTTPSConnection.connect", connected):
            connection.connect()
        connection.sock.getpeercert.assert_called_once_with(binary_form=True)
        connection.sock.sendall.assert_not_called()
        connection.close()

    def test_changed_certificate_is_rejected_and_socket_closed(self):
        connection = _PinnedConnection("files.test", certificate_sha256="0" * 64)
        sock = Mock()
        sock.getpeercert.return_value = b"different-certificate"
        def connected(_): connection.sock = sock
        with patch("http.client.HTTPSConnection.connect", connected), self.assertRaises(ssl.SSLCertVerificationError):
            connection.connect()
        sock.close.assert_called_once()
        sock.sendall.assert_not_called()

    def test_pin_matches_exact_origin_and_other_origins_use_normal_tls(self):
        handler = _PinnedHTTPSHandler({"https://files.test:8082": "a" * 64})
        request = Request("https://files.test:8082/bucket/file.pdf")
        with patch.object(handler, "do_open", return_value="pinned") as opened:
            self.assertEqual("pinned", handler.https_open(request))
            factory = opened.call_args.args[0]
            connection = factory("files.test", context=opened.call_args.kwargs["context"])
            self.assertEqual("a" * 64, connection.certificate_sha256)
        with patch.object(HTTPSHandler, "https_open", return_value="default") as normal:
            self.assertEqual("default", handler.https_open(Request("https://files.test:8443/file.pdf")))
            normal.assert_called_once()

    def test_invalid_pin_configuration_is_rejected(self):
        for value in ([], {"https://files.test": "bad"}, {"http://files.test": "a" * 64},
                      {"https://user:pass@files.test": "a" * 64}, {"https://files.test/path": "a" * 64}):
            with self.subTest(value=value), self.assertRaises(InvalidRequest):
                _PinnedHTTPSHandler(value)
