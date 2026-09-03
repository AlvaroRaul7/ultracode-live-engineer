import json


def seed_cache(cache_path, data):
    """Write raw cache JSON directly, bypassing _save_cache (so tests can
    seed exactly the shape they need without going through the real API)."""
    with open(cache_path, "w") as f:
        json.dump(data, f)


def make_gh_json(script):
    """Build a stateful fake gh_json(*args) -> (stdout, err).

    `script` is a list of (prefix, response) pairs. On each call, every
    prefix that matches args[: len(prefix)] is collected; if more than one
    prefix matches, this raises immediately (an ambiguous script is a test
    bug — two scripted prefixes both claim the same call, so whichever
    happened to be listed first would silently win). If exactly one prefix
    matches, its response is returned. Every call is recorded on the
    returned callable's `.calls` list (as the full args tuple), so a test
    can assert a PR was never queried at all (cache hit) or was queried
    exactly the calls it expected, in order.
    """
    calls = []

    def fake(*args):
        calls.append(args)
        matches = [response for prefix, response in script if args[: len(prefix)] == prefix]
        if len(matches) > 1:
            raise AssertionError(
                f"ambiguous gh_json call {args}: matched {len(matches)} scripted prefixes"
            )
        if matches:
            return matches[0]
        raise AssertionError(f"unscripted gh_json call: {args}")

    fake.calls = calls
    return fake
