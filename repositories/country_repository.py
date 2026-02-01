from typing import List, Optional
from schemas.country_schema import (
    CountryCreateRequest,
    CountryUpdateRequest,
    CountryResponse
)


class CountryRepository:

    def __init__(self, db):
        self.db = db

    def get_all(self) -> List[CountryResponse]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute("""
            SELECT Id, Name, IsoCode2, IsoCode3,
                   Capital, PhoneCode, Continent
            FROM tblCountries
            WHERE Void = 0
        """)
        rows = cursor.fetchall()

        return [
            CountryResponse(
                id=row["Id"],
                name=row["Name"],
                iso_code2=row["IsoCode2"],
                iso_code3=row["IsoCode3"],
                capital=row["Capital"],
                phone_code=row["PhoneCode"],
                continent=row["Continent"]
            )
            for row in rows
        ]

    def get_by_id(self, country_id: int) -> Optional[CountryResponse]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute("""
            SELECT Id, Name, IsoCode2, IsoCode3,
                   Capital, PhoneCode, Continent
            FROM tblCountries
            WHERE Id = %s AND Void = 0
        """, (country_id,))
        row = cursor.fetchone()

        if not row:
            return None

        return CountryResponse(
            id=row["Id"],
            name=row["Name"],
            iso_code2=row["IsoCode2"],
            iso_code3=row["IsoCode3"],
            capital=row["Capital"],
            phone_code=row["PhoneCode"],
            continent=row["Continent"]
        )

    def exists_by_id(self, country_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT 1 FROM tblCountries WHERE Id = %s AND Void = 0 LIMIT 1",
            (country_id,)
        )
        return cursor.fetchone() is not None

    def exists_by_name(self, name: str) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT 1 FROM tblCountries WHERE Name = %s AND Void = 0 LIMIT 1",
            (name,)
        )
        return cursor.fetchone() is not None

    def exists_by_iso2(self, iso_code2: str) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT 1 FROM tblCountries WHERE IsoCode2 = %s AND Void = 0 LIMIT 1",
            (iso_code2,)
        )
        return cursor.fetchone() is not None

    def exists_by_iso3(self, iso_code3: str) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT 1 FROM tblCountries WHERE IsoCode3 = %s AND Void = 0 LIMIT 1",
            (iso_code3,)
        )
        return cursor.fetchone() is not None

    def create(self, request: CountryCreateRequest) -> CountryResponse:
        cursor = self.db.cursor()

        query = """
            INSERT INTO tblCountries
            (Name, IsoCode2, IsoCode3, Capital, PhoneCode, Continent)
            VALUES (%s, %s, %s, %s, %s, %s)
        """

        cursor.execute(query, (
            request.name,
            request.iso_code2,
            request.iso_code3,
            request.capital,
            request.phone_code,
            request.continent
        ))

        self.db.commit()
        country_id = cursor.lastrowid

        return self.get_by_id(country_id)

    def update(self, country_id: int, request: CountryUpdateRequest) -> Optional[CountryResponse]:
        update_data = request.model_dump(exclude_unset=True)

        if not update_data:
            return self.get_by_id(country_id)

        field_mapping = {
            "name": "Name",
            "iso_code2": "IsoCode2",
            "iso_code3": "IsoCode3",
            "capital": "Capital",
            "phone_code": "PhoneCode",
            "continent": "Continent"
        }

        fields = []
        values = []

        for key, value in update_data.items():
            if key in field_mapping:
                fields.append(f"{field_mapping[key]} = %s")
                values.append(value)

        if not fields:
            return self.get_by_id(country_id)

        query = f"""
            UPDATE tblCountries
            SET {', '.join(fields)}
            WHERE Id = %s AND Void = 0
        """

        values.append(country_id)

        cursor = self.db.cursor()
        cursor.execute(query, tuple(values))
        self.db.commit()

        return self.get_by_id(country_id)

    def delete(self, country_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute("""
            UPDATE tblCountries
            SET Void = 1
            WHERE Id = %s AND Void = 0
        """, (country_id,))
        self.db.commit()

        return cursor.rowcount > 0