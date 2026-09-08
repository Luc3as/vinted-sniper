"""The brake on the login form: a few wrong passwords buy a wait, and the wait grows."""

from vinted_sniper.web.security import LoginThrottle


def test_a_few_wrong_passwords_do_not_block() -> None:
    throttle = LoginThrottle(attempts=5, cooldown_s=60.0)

    for i in range(4):
        assert throttle.failed("1.2.3.4", now=float(i)) == 0.0
    assert throttle.retry_after("1.2.3.4", now=4.0) == 0.0


def test_the_filling_attempt_imposes_the_cooldown() -> None:
    throttle = LoginThrottle(attempts=5, cooldown_s=60.0)

    for i in range(4):
        throttle.failed("1.2.3.4", now=float(i))
    assert throttle.failed("1.2.3.4", now=4.0) == 60.0
    assert throttle.retry_after("1.2.3.4", now=5.0) == 59.0


def test_repeat_offenders_wait_twice_as_long_each_time_up_to_a_ceiling() -> None:
    throttle = LoginThrottle(attempts=1, cooldown_s=60.0, max_cooldown_s=900.0)

    assert throttle.failed("1.2.3.4", now=0.0) == 60.0
    assert throttle.failed("1.2.3.4", now=100.0) == 120.0
    assert throttle.failed("1.2.3.4", now=300.0) == 240.0
    assert throttle.failed("1.2.3.4", now=600.0) == 480.0
    assert throttle.failed("1.2.3.4", now=1200.0) == 900.0
    assert throttle.failed("1.2.3.4", now=2200.0) == 900.0, "the ceiling holds"


def test_one_correct_sign_in_wipes_the_slate() -> None:
    throttle = LoginThrottle(attempts=1, cooldown_s=60.0)

    throttle.failed("1.2.3.4", now=0.0)
    throttle.failed("1.2.3.4", now=100.0)
    throttle.succeeded("1.2.3.4")

    assert throttle.retry_after("1.2.3.4", now=101.0) == 0.0
    assert throttle.failed("1.2.3.4", now=102.0) == 60.0, "escalation resets too"


def test_addresses_are_throttled_separately() -> None:
    throttle = LoginThrottle(attempts=1, cooldown_s=60.0)

    throttle.failed("1.2.3.4", now=0.0)
    assert throttle.retry_after("5.6.7.8", now=1.0) == 0.0


def test_old_failures_age_out_of_the_window() -> None:
    throttle = LoginThrottle(attempts=2, window_s=600.0, cooldown_s=60.0)

    throttle.failed("1.2.3.4", now=0.0)
    assert throttle.failed("1.2.3.4", now=700.0) == 0.0, "the first strike expired"
