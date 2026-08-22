# tests/common/test_logging_setup.py
"""common/logging_setup.py，對應 11a_logging_architecture.md 五章。"""
import logging
import logging.handlers

from common.logging_setup import _NOISY_THIRD_PARTY_LOGGERS, configure_logging
from common.trace_context import current_trace_id


def _reset_root_handlers():
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def test_file_handler_true_adds_rotating_file_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_LOG_PATH", str(tmp_path / "orchestrator.log"))
    _reset_root_handlers()
    try:
        configure_logging(run_id="test_run", file_handler=True)
        handlers = logging.getLogger().handlers
        assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers)
        assert (tmp_path / "orchestrator.log").exists()
    finally:
        _reset_root_handlers()


def test_file_handler_false_only_adds_console_handler():
    # llmlog CLI 用這個選項，避免跟 Orchestrator 主行程搶
    # logs/orchestrator.log 的 rollover（見五章「為什麼 llmlog 不掛
    # file handler」）。
    _reset_root_handlers()
    try:
        configure_logging(run_id="cli", file_handler=False)
        handlers = logging.getLogger().handlers
        assert not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers)
        assert any(isinstance(h, logging.StreamHandler) for h in handlers)
    finally:
        _reset_root_handlers()


def test_context_filter_injects_run_id_and_trace_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_LOG_PATH", str(tmp_path / "orchestrator.log"))
    _reset_root_handlers()
    try:
        configure_logging(run_id="run_xyz", file_handler=False)
        logger = logging.getLogger("test_context_filter")

        captured = []
        handler = logging.getLogger().handlers[0]
        original_filter = handler.filters[0]

        record = logger.makeRecord(logger.name, logging.INFO, __file__, 1, "msg", (), None)
        original_filter.filter(record)
        assert record.run_id == "run_xyz"
        assert record.trace_id == "-"  # 沒有進行中的 LLM 呼叫時是 "-"

        token = current_trace_id.set("trace_abc")
        try:
            record2 = logger.makeRecord(logger.name, logging.INFO, __file__, 1, "msg", (), None)
            original_filter.filter(record2)
            assert record2.trace_id == "trace_abc"
        finally:
            current_trace_id.reset(token)
    finally:
        _reset_root_handlers()


def test_noisy_third_party_loggers_suppressed_to_warning():
    _reset_root_handlers()
    try:
        configure_logging(run_id="test_run", file_handler=False)
        for name in _NOISY_THIRD_PARTY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING
    finally:
        _reset_root_handlers()


def test_repeated_calls_do_not_duplicate_handlers():
    _reset_root_handlers()
    try:
        configure_logging(run_id="run_1", file_handler=False)
        configure_logging(run_id="run_2", file_handler=False)
        assert len(logging.getLogger().handlers) == 1
    finally:
        _reset_root_handlers()
