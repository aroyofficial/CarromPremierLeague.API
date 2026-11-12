"""Knockout bracket decided from the league table.

Four or fewer teams: one final, 1st against 2nd.
More than four teams: an eliminator, 2nd against 3rd, then a final between
1st and the eliminator winner.

Ties are already ordered by points, net coins, head-to-head wins, then
Golden Strike wins. Teams that still share that key are tied, and the
registration order used as a last sort is not a tie-break. A Golden Strike
is scheduled only for a tie that can still change one of those places.
"""

FINAL = "final"
ELIMINATOR = "eliminator"


def tie_key(row: dict) -> tuple:
    return (
        int(row.get("Points") or 0),
        int(row.get("TotalNetPoints") or 0),
        int(row.get("HeadToHeadWins") or 0),
        int(row.get("GoldenStrikeWins") or 0),
    )


def qualification_tie_groups(ranked_rows: list, participant_count: int) -> list[list[int]]:
    # Boundaries sit between the places a tie can still change.
    # Four or fewer: 1st and 2nd. More than four: 1st, and the eliminator
    # places 2nd and 3rd, including the cut with the first team that misses out.
    boundaries = [1, 2] if participant_count <= 4 else [1, 2, 3]
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


def knockout_fixtures(
    participant_count: int,
    ranked_team_ids: list[int],
    eliminator_winner: int | None,
    ties_pending: bool,
) -> list[tuple[str, int | None, int | None]]:
    """Matches to keep, in the order they should be played.

    A side is None while that team is not known yet, so the fixture is stored
    and shown as TBD against TBD.
    """
    if participant_count < 2:
        return []

    if participant_count <= 4:
        if ties_pending or len(ranked_team_ids) < 2:
            return [(FINAL, None, None)]
        return [(FINAL, ranked_team_ids[0], ranked_team_ids[1])]

    if ties_pending or len(ranked_team_ids) < 3:
        return [(ELIMINATOR, None, None), (FINAL, None, None)]

    eliminator = (ELIMINATOR, ranked_team_ids[1], ranked_team_ids[2])
    if eliminator_winner is None:
        return [eliminator, (FINAL, None, None)]
    return [eliminator, (FINAL, ranked_team_ids[0], eliminator_winner)]
