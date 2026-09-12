"""Action verb glossary, verbatim from RoboCOIN paper Appendix A Table III.

39 verbs across 4 categories, each with synonyms and a short definition. Used
to match instruction text to canonical verbs and to validate segment.verb.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerbSpec:
    verb: str
    category: str
    synonyms: tuple[str, ...]
    definition: str


# Verbatim from arXiv:2511.17441 Appendix A, Table III.
GLOSSARY: tuple[VerbSpec, ...] = (
    # General Manipulation
    VerbSpec("pick", "General Manipulation", ("grab", "take", "get"),
             "Grasp and lift an object from its position"),
    VerbSpec("grasp", "General Manipulation", ("grip", "clutch", "hold"),
             "Firmly hold an object without positional change"),
    VerbSpec("place", "General Manipulation", ("put", "set", "position"),
             "Put an object onto a surface or target location"),
    VerbSpec("pass", "General Manipulation", ("hand over", "give"),
             "Transfer an object between two hands/end-effectors"),
    VerbSpec("rotate", "General Manipulation", ("twist", "turn", "revolve"),
             "Change the angular orientation of an object"),
    VerbSpec("push", "General Manipulation", ("slide", "shove"),
             "Move an object by applying forward force"),
    VerbSpec("pull", "General Manipulation", ("drag", "tug"),
             "Draw an object toward the agent"),
    VerbSpec("lift", "General Manipulation", ("raise", "hoist"),
             "Move an object upward to a higher position"),
    VerbSpec("store", "General Manipulation", ("stow", "deposit"),
             "Place an object inside a closed/semi-enclosed container"),
    # Object State Change
    VerbSpec("press", "Object State Change", ("depress", "push down"),
             "Apply continuous force to deform/activate an object"),
    VerbSpec("click", "Object State Change", ("tap", "press briefly"),
             "Apply momentary force to actuate a button/switch"),
    VerbSpec("open", "Object State Change", ("unfold", "unzip"),
             "Switch an object from closed to accessible state"),
    VerbSpec("unzip", "Object State Change", (),
             "Open a zippered fastener"),
    VerbSpec("unfold", "Object State Change", ("spread out", "expand"),
             "Flatten a folded or compact object"),
    VerbSpec("close", "Object State Change", ("shut", "zip"),
             "Switch an object from open to closed state"),
    VerbSpec("fold", "Object State Change", ("tuck", "collapse"),
             "Bend an object into a compact/layered form"),
    VerbSpec("unscrew", "Object State Change", ("loosen", "twist off"),
             "Release a lid/cap by counterclockwise rotation"),
    # Object Relation Change
    VerbSpec("stack", "Object Relation Change", ("pile", "heap"),
             "Arrange objects vertically in layers"),
    VerbSpec("arrange", "Object Relation Change", ("line up", "organize"),
             "Place objects in an orderly spatial pattern"),
    VerbSpec("insert", "Object Relation Change", ("put in", "slot"),
             "Place one object into another structure"),
    VerbSpec("plug_in", "Object Relation Change", ("plug in", "connect", "attach"),
             "Insert an electrical plug into a power socket"),
    VerbSpec("remove", "Object Relation Change", ("pull out", "extract"),
             "Take an object out of a space/connection"),
    VerbSpec("unplug", "Object Relation Change", ("disconnect",),
             "Remove an electrical plug from a socket"),
    VerbSpec("assemble", "Object Relation Change", ("construct", "build"),
             "Combine parts into a functional unit"),
    VerbSpec("stick", "Object Relation Change", ("paste", "attach"),
             "Affix an object to a surface via adhesion"),
    VerbSpec("swap", "Object Relation Change", ("exchange", "switch"),
             "Exchange positions of two objects"),
    # Task-Specific Actions
    VerbSpec("wipe", "Task-Specific Actions", ("scrub", "clean"),
             "Clean a surface via rubbing motion"),
    VerbSpec("erase", "Task-Specific Actions", ("remove marks",),
             "Eliminate traces or residues from a surface"),
    VerbSpec("rinse", "Task-Specific Actions", ("wash", "flush"),
             "Clean an object using flowing water"),
    VerbSpec("turn_on", "Task-Specific Actions", ("turn on", "activate", "start"),
             "Power on an electrical/mechanical device"),
    VerbSpec("turn_off", "Task-Specific Actions", ("turn off", "deactivate", "stop"),
             "Power off an electrical/mechanical device"),
    VerbSpec("pour", "Task-Specific Actions", ("transfer", "spill"),
             "Move liquid from one container to another"),
    VerbSpec("scoop", "Task-Specific Actions", ("ladle", "dig out"),
             "Lift material using a spoon or scooping tool"),
    VerbSpec("stir", "Task-Specific Actions", ("mix", "blend"),
             "Homogenize liquid/particulate matter"),
    VerbSpec("cut", "Task-Specific Actions", ("chop", "slice", "split"),
             "Separate an object using a sharp tool"),
    VerbSpec("play", "Task-Specific Actions", ("perform",),
             "Operate a musical instrument"),
    VerbSpec("make", "Task-Specific Actions", ("build", "create"),
             "Construct an object or prepare food"),
    VerbSpec("spell", "Task-Specific Actions", ("recite letters",),
             "Form words by sequencing individual characters"),
    VerbSpec("write", "Task-Specific Actions", ("inscribe", "jot"),
             "Produce characters on a surface"),
)

# Non-manipulation phase markers used when a segment is NOT a glossary verb
# (e.g., the arm is approaching, idle, or retracting).
NON_VERB_PHASES: frozenset[str] = frozenset(
    {"approach", "idle", "retract", "transit", "end"}
)

VERBS: frozenset[str] = frozenset(spec.verb for spec in GLOSSARY)
VALID_PHASES: frozenset[str] = VERBS | NON_VERB_PHASES


def canonical_verb(text: str) -> str | None:
    """Map a natural-language word/phrase to a canonical verb.

    Returns the canonical verb if `text` (lowercased) matches a verb or one of
    its synonyms exactly. Returns None otherwise. Multi-word synonyms are
    matched case-insensitively as substrings of the lowercased text.
    """
    if not text:
        return None
    lowered = text.strip().lower()
    for spec in GLOSSARY:
        if lowered == spec.verb.replace("_", " ") or lowered == spec.verb:
            return spec.verb
        for syn in spec.synonyms:
            if lowered == syn.lower():
                return spec.verb
    return None


def extract_verbs(prompt: str) -> list[str]:
    """Return canonical verbs detected in a free-form instruction string.

    Performs a simple word-and-bigram scan. Preserves order of first
    appearance and deduplicates. Conservative on purpose — used only for
    auto-populating the trajectory taxonomy, never as ground truth.
    """
    if not prompt:
        return []
    tokens = [tok.lower().strip(",.;:!?\"'()") for tok in prompt.split()]
    found: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(tokens):
        bigram = " ".join(tokens[i:i + 2]) if i + 1 < len(tokens) else ""
        verb = canonical_verb(bigram) or canonical_verb(tokens[i])
        if verb and verb not in seen:
            found.append(verb)
            seen.add(verb)
        i += 1
    return found
