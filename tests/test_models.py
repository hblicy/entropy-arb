import pytest

from entropy_arb.models import OrderResult


def test_order_result_exposes_normalized_fields():
    result = OrderResult(
        status="filled", filled_base=0.5, avg_px=100.25)
    assert result.status == "filled"
    assert result.filled_base == 0.5
    assert result.avg_px == 100.25
    assert result.err is None
    assert result.unresolved is False


def test_order_result_rejects_negative_fill():
    with pytest.raises(ValueError, match="filled_base"):
        OrderResult(status="filled", filled_base=-0.1)


def test_order_result_rejects_nonpositive_average_price():
    with pytest.raises(ValueError, match="avg_px"):
        OrderResult(status="filled", filled_base=0.1, avg_px=0.0)


def test_order_result_marks_rate_limit_errors_explicitly():
    result = OrderResult.send_failed("RATE_LIMITED: HTTP 429")
    assert result.status == "send-failed"
    assert result.rate_limited is True
