"""Transition techniques: what each one is, how to perform it, and when the planner offers it."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Technique:
    key: str
    name: str
    family: str          # blend | fx | cut  (fx = cut family performed with an effect)
    how: str
    out_word: str = ""   # label on the OUT DONE cue for cut-family moves


TECHNIQUES: dict[str, Technique] = {t.key: t for t in [
    Technique("vocalover", "Vocal over", "blend",
              "A's chorus keeps singing over B's build. B in with lows cut; on CUT swap the bass and pull A."),
    Technique("handoff", "Vocal handoff", "blend",
              "A is instrumental here, B's vocal starts on top of it. Ride B up early, swap bass and pull A on CUT."),
    Technique("bassswap", "Bass swap", "blend",
              "Classic EQ blend (4 or 8 bars). B in with lows cut; on the CUT downbeat kill A's lows, open B's, fade A."),
    Technique("filter", "Filter ride", "fx",
              "B starts high-passed and opens across the blend; A gets low-passed on the way out. Hides vocal and key rub."),
    Technique("tease", "Intro tease", "blend",
              "B's build has no bass, so it sits under A's last 8 with no EQ work. B's drop lands as A leaves."),
    Technique("echo", "Echo out", "fx",
              "Echo / delay at 1/2 beat on A's last bar, pull A on CUT, B starts on the 1 at its own tempo.", "ECHO OUT"),
    Technique("wash", "Reverb wash", "fx",
              "Big reverb on A on CUT and let the tail ring into B's first bar.", "WASH OUT"),
    Technique("roll", "Loop roll", "fx",
              "Loop A's last bar: 1, 1/2, 1/4, 1/8 across the 4 bars before CUT, add high-pass, release into B.", "ROLL OUT"),
    Technique("cut", "Drop swap", "cut",
              "Clean cut on the downbeat, no FX. A out and B's hook in on the same 1.", "CUT"),
    Technique("brake", "Brake", "cut",
              "Vinyl brake on A on CUT (about a beat), B's hook on the next 1. For the biggest tempo drops.", "BRAKE"),
    Technique("stop", "Dead stop", "cut",
              "One beat of silence after A, then B's drop. Pure contrast, used once.", "DEAD STOP"),
]}


def options(family: str, a_vocal: float, b_vocal: float, b_bass: float, bpm_delta: float) -> list[str]:
    """Techniques that suit a transition, best first (before variety rules are applied)."""
    if family == "blend":
        if a_vocal >= 50 and b_vocal < 50:
            return ["vocalover", "bassswap", "filter"]
        if a_vocal < 50 and b_vocal >= 50:
            return ["handoff", "filter", "bassswap"]
        if a_vocal < 50 and b_vocal < 50:
            return (["tease"] if b_bass < .5 else []) + ["bassswap", "filter"]
        return ["filter", "bassswap"]
    if abs(bpm_delta) >= 15:
        return ["brake", "echo", "cut"]
    if a_vocal >= 50:
        return ["echo", "wash", "roll", "cut"]
    return ["roll", "cut", "echo"]


def deck_steps(tech: str, family: str, overlap: int, a_title: str, b_title: str, a_out_start: int, a_out_done: int,
               b_in: int, b_land: int, bpm_a: float, bpm_b: float) -> list[str]:
    """Step-by-step deck instructions for auditioning one transition in rekordbox."""
    t = TECHNIQUES[tech]
    steps = [f"Deck 1: load {a_title}, jump to the red OUT START memory cue (bar {a_out_start}).",
             f"Deck 2: load {b_title}, jump to its green IN cue (bar {b_in})."]
    if family == "blend":
        steps += [f"Set deck 2's tempo to {bpm_a:g} (Sync on deck 2 only), lows on deck 2 fully cut.",
                  f"Play deck 1. On the downbeat of OUT START start deck 2: {overlap} bars of overlap.",
                  f"{t.name}: {t.how}",
                  f"By OUT DONE (A bar {a_out_done}) deck 1 is gone and B hits its drop at bar {b_land}."]
        if abs(bpm_b - bpm_a) >= 1:
            steps.append(f"After the blend ease deck 2 back toward {bpm_b:g} BPM over 16-32 bars.")
    else:
        steps += [f"Leave deck 2 at its own {bpm_b:g} BPM; no sync needed.",
                  f"Play deck 1 from OUT START; you have 8 bars to get ready.",
                  f"{t.name}: {t.how}",
                  f"On OUT DONE (A bar {a_out_done}) start deck 2 from bar {b_land} on the 1."]
    steps.append("Rate it: put a GREEN hot cue near A's OUT DONE if it works, RED if it doesn't, "
                 "then run collect_ratings.")
    return steps
