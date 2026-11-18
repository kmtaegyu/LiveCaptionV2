import logging
from pathlib import Path
import tempfile
import unittest

from app_logging import configure_logging, SafeFormatter, redact_urls


class LoggingTest(unittest.TestCase):
    def test_url_redaction_in_tracebacks(self):
        url = "https://user:password@example.com/live.m3u8?token=secret#fragment"
        self.assertEqual(redact_urls(url), "https://example.com/live.m3u8?REDACTED")
        try:
            raise ValueError(url)
        except ValueError:
            import sys
            record = logging.LogRecord("test", logging.ERROR, "", 0, "failure %s", (url,), sys.exc_info())
        text = SafeFormatter("%(message)s").format(record)
        self.assertNotIn("secret", text)
        self.assertNotIn("password", text)
        self.assertNotIn("#fragment", text)
        self.assertIn("ValueError", text)

    def test_file_utf8_and_rotation(self):
        logger = logging.getLogger("livecaption")
        previous_handlers = list(logger.handlers)
        previous_level, previous_propagate = logger.level, logger.propagate
        for handler in previous_handlers:
            logger.removeHandler(handler)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "logs" / "test.log"
                configure_logging(path)
                handler = logger.handlers[0]
                handler.maxBytes = 400
                logger.info("한글 테스트 https://example.com/video?token=secret")
                handler.flush()
                text = path.read_text(encoding="utf-8")
                self.assertIn("한글 테스트", text)
                self.assertNotIn("secret", text)
                for _ in range(20):
                    logger.info("rotation record " * 5)
                handler.flush()
                self.assertTrue(path.with_name("test.log.1").exists())
                self.assertLessEqual(len(list(path.parent.glob("test.log*"))), 4)
                logger.removeHandler(handler)
                handler.close()
        finally:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            for handler in previous_handlers:
                logger.addHandler(handler)
            logger.setLevel(previous_level)
            logger.propagate = previous_propagate
