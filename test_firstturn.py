import json

from firstturn import check, load

EXPECT = {
    "clean-de-consent": ("PASS", set()),
    "clean-en": ("PASS", set()),
    "caller-first-skipped-greeting": ("FAIL", {"topic-before-disclosure", "data-before-disclosure"}),
    "ios-screening": ("FAIL", {"disclosed-to-screener", "topic-before-disclosure"}),
    "barge-in": ("FAIL", {"disclosure-cut-off", "topic-before-disclosure"}),
    "data-first": ("FAIL", {"topic-before-disclosure", "data-before-disclosure"}),
    "asked-deflected": ("FAIL", {"asked-not-told"}),
    "recorded-no-yes": ("FAIL", {"recorded-no-consent"}),
    "recorded-after-no": ("FAIL", {"recorded-after-refusal"}),
    "voicemail-no-disclosure": ("WARN", {"voicemail-no-disclosure"}),
    "voicemail-clean": ("PASS", set()),
    "late-but-before-topic": ("WARN", {"outside-first-four"}),
    "caller-first-handled": ("PASS", set()),
    "recording-late-notice": ("WARN", {"recording-notice-late"}),
    "topic-in-same-breath": ("FAIL", {"topic-before-disclosure"}),
    "clean-de-inbound-consent": ("PASS", set()),
    "clean-en-virtual": ("PASS", set()),
    "not-reached": ("SKIP", {"not-connected"}),
}


def test_demo_calls():
    calls = load("demo/calls.json")
    assert len(calls) == len(EXPECT)
    for call in calls:
        verdict, findings, _ = check(call)
        assert (verdict, {f[1] for f in findings}) == EXPECT[call["_label"]], call["_label"]


if __name__ == "__main__":
    test_demo_calls()
    print("ok")
