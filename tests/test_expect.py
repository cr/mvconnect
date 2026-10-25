"""The test helpers themselves: a thing is found as a whole word or number, wherever a sentence puts it."""

from expect import has, line_with


def test_a_thing_is_a_whole_word_or_number():
    assert has("the maximum is 48", 48) and not has("the maximum is 148", 48) and not has("12.1 V", 2.1)
    assert not has("17 frames", 7) and not has("not now", "no") and not has("v1.48", 48)


def test_a_full_stop_ends_a_word_but_a_decimal_point_does_not():
    assert has("the maximum is 48.", 48) and has("device 1b:0cad9.", "1b:0cad9") and has("see #7.", "#7")
    assert not has("the maximum is 48.5", 48) and line_with("a\nthe maximum is 48.\n", "maximum", 48)
