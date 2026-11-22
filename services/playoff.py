"""Knockout bracket decided from the league table.

Four or fewer teams: one final, 1st against 2nd.
Five to seven teams: 1st goes straight to the final. 2nd plays 3rd in an
eliminator and the winner meets 1st in the final.
Eight or more teams: Qualifier 1 is 1st against 2nd and the eliminator is 3rd
against 4th. The eliminator winner plays the Qualifier 1 loser in Qualifier 2.
The Qualifier 1 winner plays the Qualifier 2 winner in the final.

A side is None while that team is not known yet, so the fixture is stored and
shown as TBD for that side. A team is placed as soon as its place cannot change,
and the rest stay TBD until the table or an earlier knockout match decides them.

Ties are already ordered by points, net coins, head-to-head wins, then
Golden Strike wins. Teams that still share that key are tied, and the
registration order used as a last sort is not a tie-break. A Golden Strike
is scheduled only for a tie that can still change one of those places.
"""

FINAL = "final"
ELIMINATOR = "eliminator"
QUALIFIER1 = "qualifier1"
QUALIFIER2 = "qualifier2"

FORMAT_FINAL = "final"
FORMAT_ELIMINATOR = "eliminator"
FORMAT_FULL = "full"

FULL_BRACKET_MIN_TEAMS = 8
# Most league points a team can win from one match.
WIN_POINTS = 2

SLOT_LABELS = {
    QUALIFIER1: "Qualifier 1",
    ELIMINATOR: "Eliminator",
    QUALIFIER2: "Qualifier 2",
    FINAL: "Final",
}

# Where each side of a match comes from, in the order the matches are played.
BLUEPRINTS = {
    FORMAT_FINAL: [
        (FINAL, ("#1", "#2")),
    ],
    FORMAT_ELIMINATOR: [
        (ELIMINATOR, ("#2", "#3")),
        (FINAL, ("#1", "Winner of Eliminator")),
    ],
    FORMAT_FULL: [
        (QUALIFIER1, ("#1", "#2")),
        (ELIMINATOR, ("#3", "#4")),
        (QUALIFIER2, ("Winner of Eliminator", "Loser of Qualifier 1")),
        (FINAL, ("Winner of Qualifier 1", "Winner of Qualifier 2")),
    ],
}


def bracket_format(participant_count: int) -> str | None:
    if participant_count < 2:
        return None
    if participant_count <= 4:
        return FORMAT_FINAL
    if participant_count < FULL_BRACKET_MIN_TEAMS:
        return FORMAT_ELIMINATOR
    return FORMAT_FULL


def seeds_needed(participant_count: int) -> int:
    """How many table places the bracket reads."""
    return {FORMAT_FINAL: 2, FORMAT_ELIMINATOR: 3, FORMAT_FULL: 4}.get(bracket_format(participant_count), 0)


def tie_key(row: dict) -> tuple:
    return (
        int(row.get("Points") or 0),
        int(row.get("TotalNetPoints") or 0),
        int(row.get("HeadToHeadWins") or 0),
        int(row.get("GoldenStrikeWins") or 0),
    )


def qualification_tie_groups(ranked_rows: list, participant_count: int) -> list[list[int]]:
    # Boundaries sit between the places a tie can still change.
    # Four or fewer: 1st and 2nd. Five to seven: 1st, and the eliminator
    # places 2nd and 3rd. Eight or more: the four places that feed Qualifier 1
    # and the eliminator. Each includes the cut with the first team that misses out.
    if participant_count <= 4:
        boundaries = [1, 2]
    elif participant_count < FULL_BRACKET_MIN_TEAMS:
        boundaries = [1, 2, 3]
    else:
        boundaries = [1, 2, 3, 4]
    groups: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()

    for boundary in boundaries:
        if boundary >= len(ranked_rows):
            continue
        if tie_key(ranked_rows[boundary - 1]) != tie_key(ranked_rows[boundary]):
            continue

        start = boundary - 1
        end = boundary
        while start > 0 and tie_key(ranked_rows[start - 1]) == tie_key(ranked_rows[boundary]):
            start -= 1
        while end + 1 < len(ranked_rows) and tie_key(ranked_rows[end + 1]) == tie_key(ranked_rows[boundary]):
            end += 1

        group = tuple(sorted(int(row["TeamId"]) for row in ranked_rows[start : end + 1]))
        if group not in seen:
            seen.add(group)
            groups.append(list(group))
    return groups


def seeds_after_ties(ranked_team_ids: list[int], tie_groups: list[list[int]], needed: int) -> list[int | None]:
    """The first `needed` places, with None where a tie still decides who holds the place."""
    seeds: list[int | None] = [ranked_team_ids[i] if i < len(ranked_team_ids) else None for i in range(needed)]
    tied = {team_id for group in tie_groups for team_id in group}
    return [None if team_id in tied else team_id for team_id in seeds]


def locked_seeds(ranked_rows: list, remaining_by_team: dict, needed: int) -> list[int | None]:
    """Places that no remaining league result can change, read from a table that is still being played.

    A team holds its place when every team above it already has more points than it can reach, and every
    team below it can no longer reach its points. The comparison is strict, so a possible tie keeps the place open.
    """
    teams = [(int(row["TeamId"]), int(row.get("Points") or 0)) for row in ranked_rows]
    most = {team_id: points + WIN_POINTS * int(remaining_by_team.get(team_id, 0)) for team_id, points in teams}
    seeds: list[int | None] = []
    for place in range(needed):
        if place >= len(teams):
            seeds.append(None)
            continue
        team_id, points = teams[place]
        above_safe = all(other_points > most[team_id] for _, other_points in teams[:place])
        below_safe = all(most[other_id] < points for other_id, _ in teams[place + 1 :])
        seeds.append(team_id if above_safe and below_safe else None)
    return seeds


def knockout_fixtures(
    participant_count: int,
    ranked_team_ids: list,
    eliminator_winner: int | None,
    ties_pending: bool,
    results: dict | None = None,
) -> list[tuple[str, int | None, int | None]]:
    """Matches to keep, in the order they should be played.

    `ranked_team_ids` lists the places in order. An entry may be None for a place that is not settled.
    `results` maps a slot to (winner, loser) for a knockout match that has been played, so the winners
    and losers move on to the next match. `eliminator_winner` is the same for the eliminator alone.
    """
    kind = bracket_format(participant_count)
    if kind is None:
        return []

    results = results or {}

    def place(number: int) -> int | None:
        if ties_pending or number > len(ranked_team_ids):
            return None
        return ranked_team_ids[number - 1]

    def outcome(slot: str) -> tuple[int | None, int | None]:
        winner, loser = results.get(slot, (None, None))
        return winner, loser

    eliminator_won = eliminator_winner if eliminator_winner is not None else outcome(ELIMINATOR)[0]

    if kind == FORMAT_FINAL:
        return [(FINAL, place(1), place(2))]

    if kind == FORMAT_ELIMINATOR:
        return [
            (ELIMINATOR, place(2), place(3)),
            (FINAL, place(1), eliminator_won),
        ]

    qualifier1_won, qualifier1_lost = outcome(QUALIFIER1)
    qualifier2_won, _ = outcome(QUALIFIER2)
    return [
        (QUALIFIER1, place(1), place(2)),
        (ELIMINATOR, place(3), place(4)),
        (QUALIFIER2, eliminator_won, qualifier1_lost),
        (FINAL, qualifier1_won, qualifier2_won),
    ]
