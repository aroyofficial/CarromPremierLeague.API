"""League fixture order for one season.

Every pair of teams plays once. A team is kept off the next match when another
remaining pairing allows it. A started league match is not paired again.
"""

from datetime import date


SCHEDULED = 1
IN_PROGRESS = 2
PLAYED = 3
LEAGUE = 1

UPCOMING_REQUIRED = "Matches can be scheduled only for an upcoming season."
TEAMS_REQUIRED = "A season needs at least four teams before matches can be scheduled."


def _shares(left, right) -> bool:
    return bool(set(left) & set(right))


def _circle_rounds(team_ids: list[int]) -> list[list[tuple[int, int]]]:
    teams: list[int | None] = list(team_ids)
    if len(teams) % 2 == 1:
        teams.append(None)
    fixed = teams[0]
    others = teams[1:]
    count = len(teams)
    half = count // 2
    rounds = []
    for _ in range(count - 1):
        ring = [fixed, *others]
        pairings = []
        for index in range(half):
            home, away = ring[index], ring[count - 1 - index]
            if home is None or away is None:
                continue
            pairings.append((int(home), int(away)))
        rounds.append(pairings)
        others = [others[-1], *others[:-1]]
    return rounds


def _rotate_away(matches, previous):
    if not previous:
        return list(matches)
    for index, match in enumerate(matches):
        if not _shares(previous, match):
            return list(matches[index:]) + list(matches[:index])
    return list(matches)


def _separate(sequence: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = list(sequence)
    for index in range(1, len(ordered)):
        if not _shares(ordered[index - 1], ordered[index]):
            continue
        for later in range(index + 1, len(ordered)):
            candidate = ordered[later]
            if later - 1 == index:
                continue
            if _shares(ordered[index - 1], candidate):
                continue
            if index + 1 < len(ordered) and _shares(candidate, ordered[index + 1]):
                continue
            displaced = ordered[index]
            if _shares(displaced, ordered[later - 1]):
                continue
            if later + 1 < len(ordered) and _shares(displaced, ordered[later + 1]):
                continue
            ordered[index], ordered[later] = ordered[later], ordered[index]
            break
    return ordered


def league_pairings(team_ids, blocked=(), previous=()) -> list[tuple[int, int]]:
    teams = sorted({int(team_id) for team_id in team_ids})
    blocked_pairs = {frozenset(int(team) for team in pair) for pair in blocked}
    sequence = []
    prior = tuple(int(team) for team in previous)
    for round_matches in _circle_rounds(teams):
        kept = [match for match in round_matches if frozenset(match) not in blocked_pairs]
        if not kept:
            continue
        ordered = _rotate_away(kept, prior)
        sequence.extend(ordered)
        prior = ordered[-1]
    return _separate(sequence)


def _kept_matches(matches):
    return [match for match in matches if int(match.status) != SCHEDULED]


def plan_league_schedule(team_ids, matches, start_date: date | None, today: date):
    if start_date is None or start_date <= today:
        raise ValueError(UPCOMING_REQUIRED)
    teams = sorted({int(team_id) for team_id in team_ids})
    if len(teams) < 4:
        raise ValueError(TEAMS_REQUIRED)

    blocked = []
    previous = ()
    latest_order = None
    for match in _kept_matches(matches):
        if latest_order is None or int(match.order) >= latest_order:
            latest_order = int(match.order)
            if match.team1 is not None and match.team2 is not None:
                previous = (int(match.team1), int(match.team2))
        if int(match.category) != LEAGUE or int(match.status) not in (IN_PROGRESS, PLAYED):
            continue
        if match.team1 is None or match.team2 is None or int(match.team1) == int(match.team2):
            continue
        blocked.append((int(match.team1), int(match.team2)))

    return league_pairings(teams, blocked, previous)
