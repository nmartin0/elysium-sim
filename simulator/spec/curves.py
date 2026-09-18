"""
curves.py  (the shape of a day)

The smallest section, and the one that does the most for how a
simulated business FEELS. A pack declares twenty-four multipliers and
an event's arrival rate is scaled by whichever hour it is, so a
plumbing firm gets calls in the morning and a bar gets them at eleven
at night.

The length is checked because twenty-three numbers would silently
shift every hour after the gap, and the sum is checked against zero
because a curve of all zeroes is an event that never fires -- which
looks exactly like a rate of zero and is a far harder thing to find.
"""


from simulator.scheduler import validate_curve
from simulator.spec.model import Curve
from simulator.spec.values import PackError


def _load_curves(raw: dict) -> dict[str, Curve]:
    curves = {}
    for name, weights in raw.items():
        path = f"curves.{name}"
        if not isinstance(weights, list):
            raise PackError(path, "a curve must be a list of 24 hourly weights")
        try:
            curves[name] = validate_curve(name, weights)
        except (ValueError, TypeError) as error:
            raise PackError(path, str(error)) from error
    return curves
