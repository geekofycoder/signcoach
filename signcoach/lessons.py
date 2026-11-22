"""Curriculum: static ASL handshapes the webcam coach can judge from a single frame.

Motion signs (HELLO, THANK YOU, ...) need several frames and are not included yet.
Descriptions are what Claude is told to compare against, so keep them concrete and
about hand shape only.
"""

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Sign:
    id: str
    label: str  # what the learner sees, e.g. "Letter A"
    description: str  # what a correct handshape looks like (given to the judge)
    tip: str  # a short spoken-friendly teaching line
    # Fingers that matter most, used for client-side highlighting when the judge
    # does not name a specific one.
    key_parts: tuple[str, ...] = field(default_factory=tuple)
    # Reference picture shown on the page (served from /static). Empty means none.
    image: str = ""
    image_credit: str = ""  # shown under the picture; the CC BY-SA files require it
    image_credit_url: str = ""


_BASE_SIGNS: list[Sign] = [
    Sign(
        "A", "Letter A",
        "Closed fist with the thumb resting straight along the side of the index finger, "
        "pointing up. The thumb is NOT tucked across the fingers. Palm faces forward.",
        "Make a fist, then let your thumb rest against the side of your index finger.",
        ("thumb",),
    ),
    Sign(
        "B", "Letter B",
        "Four fingers straight up and pressed together, thumb folded across the palm. "
        "Palm faces forward.",
        "Hold your four fingers straight up together and fold your thumb across your palm.",
        ("fingers", "thumb"),
    ),
    Sign(
        "C", "Letter C",
        "Fingers and thumb curved together into the shape of the letter C, "
        "with the side of the hand facing the viewer.",
        "Curve your whole hand like you are holding a cup.",
        ("fingers", "thumb"),
    ),
    Sign(
        "L", "Letter L",
        "Index finger pointing up and thumb pointing out sideways, forming a right angle "
        "(an L). The other three fingers are curled down. Palm faces forward.",
        "Point your index finger up and your thumb out to the side, like an L.",
        ("thumb", "index"),
    ),
    Sign(
        "O", "Letter O",
        "All fingertips curved down to touch the thumb tip, forming a round O shape.",
        "Bring all your fingertips to meet your thumb to make a round O.",
        ("fingers", "thumb"),
    ),
    Sign(
        "V", "Letter V",
        "Index and middle fingers extended and spread in a V. Ring and pinky are curled "
        "down, held by the thumb. Palm faces forward.",
        "Raise your index and middle fingers in a V and hold the others down with your thumb.",
        ("index", "middle"),
    ),
    Sign(
        "W", "Letter W",
        "Index, middle and ring fingers extended and spread apart. The thumb holds the "
        "pinky down. Palm faces forward.",
        "Spread three fingers, index, middle and ring, and hold your pinky down with your thumb.",
        ("ring", "pinky"),
    ),
    Sign(
        "Y", "Letter Y",
        "Thumb and pinky extended outward. Index, middle and ring fingers curled into "
        "the palm.",
        "Stick out only your thumb and little finger, like a phone call sign.",
        ("thumb", "pinky"),
    ),
    Sign(
        "ILY", "I love you",
        "Thumb, index finger and pinky extended. Middle and ring fingers folded down. "
        "Palm faces forward.",
        "Raise your thumb, index and pinky, and fold your middle and ring fingers.",
        ("middle", "ring"),
    ),
    Sign(
        "5", "Number 5",
        "Open hand with all five fingers spread apart and extended. Palm faces forward.",
        "Open your hand wide and spread all five fingers.",
        ("fingers",),
    ),
]

_COMMONS = "https://commons.wikimedia.org/wiki/File:"
_PUBLIC_DOMAIN = "Public domain, wpclipart.com via Wikimedia Commons"

# id -> (file in static/signs, credit text, credit link). See static/signs/CREDITS.md.
_IMAGES = {
    **{
        letter: (f"{letter}.svg", _PUBLIC_DOMAIN, f"{_COMMONS}Sign_language_{letter}.svg")
        for letter in "ABCLOVWY"
    },
    "ILY": ("ILY.svg", "Drawing by LiliCharlie, CC BY-SA 4.0, via Wikimedia Commons", f"{_COMMONS}ILY.svg"),
    "5": ("5.jpg", "Drawing by Snailsmakemehappy, CC BY-SA 4.0, via Wikimedia Commons",
          f"{_COMMONS}Sign_language_5.jpg"),
}


def _with_image(sign: Sign) -> Sign:
    entry = _IMAGES.get(sign.id)
    if entry is None:
        return sign
    file, credit, url = entry
    return replace(sign, image=f"/static/signs/{file}", image_credit=credit, image_credit_url=url)


SIGNS: list[Sign] = [_with_image(s) for s in _BASE_SIGNS]

BY_ID = {s.id: s for s in SIGNS}


class LessonState:
    """Where the learner is in the curriculum. Pure data, no I/O."""

    def __init__(self, signs: list[Sign] | None = None):
        self.signs = signs or SIGNS
        self.index = 0
        self.completed: list[str] = []

    @property
    def current(self) -> Sign:
        return self.signs[self.index]

    @property
    def finished(self) -> bool:
        return self.index >= len(self.signs)

    def advance(self) -> Sign | None:
        """Mark the current sign done and move on. Returns the next sign, or None at the end."""
        if not self.finished:
            self.completed.append(self.current.id)
        self.index += 1
        if self.index >= len(self.signs):
            self.index = len(self.signs) - 1
            return None
        return self.current

    def skip(self) -> Sign | None:
        """Move on without marking the sign completed. None if already on the last sign."""
        if self.index + 1 >= len(self.signs):
            return None
        self.index += 1
        return self.current

    def select(self, sign_id: str) -> Sign | None:
        for i, s in enumerate(self.signs):
            if s.id.lower() == sign_id.lower():
                self.index = i
                return s
        return None

    def progress(self) -> dict:
        return {
            "index": self.index,
            "total": len(self.signs),
            "completed": list(self.completed),
        }
