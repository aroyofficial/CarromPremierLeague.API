from typing import Optional, List
from schemas.team_schema import (
    TeamCreateRequest,
    TeamUpdateRequest,
    TeamResponse
)


class TeamRepository:

    def __init__(self, db):
        self.db = db

    def get_all(self) -> List[TeamResponse]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute("""
            SELECT Id, Name, Slogan, LogoUrl
            FROM tblTeams
            WHERE Void = 0
        """)
        rows = cursor.fetchall()

        return [
            TeamResponse(
                id=row["Id"],
                name=row["Name"],
                slogan=row["Slogan"],
                logo_url=row["LogoUrl"]
            )
            for row in rows
        ]

    def get_by_id(self, team_id: int) -> Optional[TeamResponse]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute("""
            SELECT Id, Name, Slogan, LogoUrl
            FROM tblTeams
            WHERE Id = %s AND Void = 0
        """, (team_id,))
        row = cursor.fetchone()

        if not row:
            return None

        return TeamResponse(
            id=row["Id"],
            name=row["Name"],
            slogan=row["Slogan"],
            logo_url=row["LogoUrl"]
        )

    def create(self, request: TeamCreateRequest) -> TeamResponse:
        cursor = self.db.cursor()

        query = """
            INSERT INTO tblTeams (Name, Slogan, LogoUrl)
            VALUES (%s, %s, %s)
        """

        cursor.execute(query, (
            request.name,
            request.slogan,
            request.logo_url
        ))

        self.db.commit()
        team_id = cursor.lastrowid

        return self.get_by_id(team_id)

    def update(self, team_id: int, request: TeamUpdateRequest) -> Optional[TeamResponse]:
        update_data = request.model_dump(exclude_unset=True)

        if not update_data:
            return self.get_by_id(team_id)

        field_mapping = {
            "name": "Name",
            "slogan": "Slogan",
            "logo_url": "LogoUrl"
        }

        fields = []
        values = []

        for key, value in update_data.items():
            if key in field_mapping:
                fields.append(f"{field_mapping[key]} = %s")
                values.append(value)

        if not fields:
            return self.get_by_id(team_id)

        query = f"""
            UPDATE tblTeams
            SET {', '.join(fields)}
            WHERE Id = %s AND Void = 0
        """

        values.append(team_id)

        cursor = self.db.cursor()
        cursor.execute(query, tuple(values))
        self.db.commit()

        return self.get_by_id(team_id)

    def delete(self, team_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute("""
            UPDATE tblTeams
            SET Void = 1
            WHERE Id = %s AND Void = 0
        """, (team_id,))
        self.db.commit()

        return cursor.rowcount > 0
