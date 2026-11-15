class GalleryRepository:
    """Season media. Public reads see published items. A season Show flag does not hide its gallery."""

    def __init__(self, db):
        self.db = db

    def known_seasons(self, season_ids: list[int]) -> set[int]:
        if not season_ids:
            return set()
        cursor = self.db.cursor()
        marks = ", ".join(["%s"] * len(season_ids))
        cursor.execute(
            f"SELECT Id FROM tblSeasons WHERE Void = 0 AND Id IN ({marks})",
            tuple(season_ids),
        )
        return {int(row["Id"]) for row in cursor.fetchall()}

    def season_is_public(self, season_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT TOP (1) Id FROM tblSeasons WHERE Id = %s AND Void = 0",
            (season_id,),
        )
        return cursor.fetchone() is not None

    def insert(self, row: dict) -> int:
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblGalleryItems
                (SourceType, MediaType, Url, StorageKey, Width, Height, Caption, AltText, TakenOn, IsPublished, CreatedBy, UpdatedBy)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s)
            """,
            (
                row["source_type"],
                row.get("media_type") or "image",
                row["url"],
                row["storage_key"],
                row["width"],
                row["height"],
                row["caption"],
                row["alt_text"],
                row["taken_on"],
                row["admin_id"],
                row["admin_id"],
            ),
        )
        return int(cursor.lastrowid)

    def replace_seasons(self, item_id: int, season_ids: list[int]) -> None:
        cursor = self.db.cursor()
        cursor.execute("SELECT SeasonId FROM tblGalleryItemSeasons WHERE ItemId = %s", (item_id,))
        current = {int(row["SeasonId"]) for row in cursor.fetchall()}
        wanted = list(dict.fromkeys(season_ids))
        for season_id in current - set(wanted):
            cursor.execute(
                "DELETE FROM tblGalleryItemSeasons WHERE ItemId = %s AND SeasonId = %s",
                (item_id, season_id),
            )
        for season_id in wanted:
            if season_id in current:
                continue
            cursor.execute(
                """
                INSERT INTO tblGalleryItemSeasons (ItemId, SeasonId, SortOrder, IsCover)
                VALUES (
                    %s,
                    %s,
                    (SELECT ISNULL(MAX(SortOrder), -1) + 1 FROM tblGalleryItemSeasons WHERE SeasonId = %s),
                    0
                )
                """,
                (item_id, season_id, season_id),
            )

    def add_seasons(self, item_ids: list[int], season_ids: list[int]) -> int:
        added = 0
        for item_id in item_ids:
            if self.get(item_id) is None:
                continue
            before = self.season_ids_for([item_id]).get(item_id, [])
            missing = [season_id for season_id in season_ids if season_id not in before]
            if not missing:
                continue
            self.replace_seasons(item_id, list(before) + missing)
            added += 1
        return added

    def get(self, item_id: int) -> dict | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, SourceType, MediaType, Url, StorageKey, Width, Height, Caption, AltText, TakenOn, IsPublished
            FROM tblGalleryItems
            WHERE Id = %s AND Void = 0
            """,
            (item_id,),
        )
        row = cursor.fetchone()
        return row

    def get_public(self, item_id: int) -> dict | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT gi.Id, gi.SourceType, gi.MediaType, gi.Url, gi.StorageKey, gi.Width, gi.Height,
                   gi.Caption, gi.AltText, gi.TakenOn, gi.IsPublished,
                   CAST(0 AS INT) AS SortOrder, CAST(0 AS BIT) AS IsCover
            FROM tblGalleryItems gi
            WHERE gi.Id = %s AND gi.Void = 0 AND gi.IsPublished = 1
              AND EXISTS (
                  SELECT 1
                  FROM tblGalleryItemSeasons gis
                  JOIN tblSeasons s ON s.Id = gis.SeasonId AND s.Void = 0
                  WHERE gis.ItemId = gi.Id
              )
            """,
            (item_id,),
        )
        return cursor.fetchone()

    def update(self, item_id: int, fields: dict, admin_id: int) -> None:
        if not fields:
            return
        assignments = [f"{column} = %s" for column in fields]
        params = list(fields.values())
        assignments.append("UpdatedBy = %s")
        assignments.append("UpdatedAt = SYSUTCDATETIME()")
        params.extend([admin_id, item_id])
        cursor = self.db.cursor()
        cursor.execute(
            f"UPDATE tblGalleryItems SET {', '.join(assignments)} WHERE Id = %s AND Void = 0",
            tuple(params),
        )

    def set_published(self, item_ids: list[int], published: bool, admin_id: int) -> int:
        if not item_ids:
            return 0
        marks = ", ".join(["%s"] * len(item_ids))
        cursor = self.db.cursor()
        cursor.execute(
            f"""
            UPDATE tblGalleryItems
            SET IsPublished = %s, UpdatedBy = %s, UpdatedAt = SYSUTCDATETIME()
            WHERE Void = 0 AND Id IN ({marks})
            """,
            (1 if published else 0, admin_id, *item_ids),
        )
        return int(cursor.rowcount or 0)

    def delete(self, item_id: int) -> dict | None:
        row = self.get(item_id)
        if row is None:
            return None
        cursor = self.db.cursor()
        cursor.execute("DELETE FROM tblGalleryItemSeasons WHERE ItemId = %s", (item_id,))
        cursor.execute("DELETE FROM tblGalleryItems WHERE Id = %s", (item_id,))
        return row

    def delete_many(self, item_ids: list[int]) -> list[dict]:
        rows = []
        for item_id in item_ids:
            row = self.delete(item_id)
            if row is not None:
                rows.append(row)
        return rows

    def season_ids_for(self, item_ids: list[int], *, public_only: bool = False) -> dict[int, list[int]]:
        if not item_ids:
            return {}
        marks = ", ".join(["%s"] * len(item_ids))
        shown = ""
        cursor = self.db.cursor()
        cursor.execute(
            f"""
            SELECT gis.ItemId, gis.SeasonId
            FROM tblGalleryItemSeasons gis
            JOIN tblSeasons s ON s.Id = gis.SeasonId AND s.Void = 0 {shown}
            WHERE gis.ItemId IN ({marks})
            ORDER BY gis.SeasonId
            """,
            tuple(item_ids),
        )
        grouped: dict[int, list[int]] = {}
        for row in cursor.fetchall():
            grouped.setdefault(int(row["ItemId"]), []).append(int(row["SeasonId"]))
        return grouped

    def list_public(self, season_id: int, after_sort, after_id, limit: int) -> list[dict]:
        # Same order as services.gallery_service.page_by_position: (SortOrder, Id).
        # SQL Server cannot type a NULL compared with an INT, so the first page omits the cursor predicate.
        cursor = self.db.cursor()
        after = ""
        params: list = [season_id]
        if after_sort is not None:
            after = "AND (gis.SortOrder > %s OR (gis.SortOrder = %s AND gi.Id > %s))"
            params.extend([after_sort, after_sort, after_id])
        params.append(limit)
        cursor.execute(
            f"""
            SELECT gi.Id, gi.SourceType, gi.MediaType, gi.Url, gi.StorageKey, gi.Width, gi.Height,
                   gi.Caption, gi.AltText, gi.TakenOn, gi.IsPublished, gis.SortOrder, gis.IsCover
            FROM tblGalleryItemSeasons gis
            JOIN tblGalleryItems gi ON gi.Id = gis.ItemId AND gi.Void = 0 AND gi.IsPublished = 1
            JOIN tblSeasons s ON s.Id = gis.SeasonId AND s.Void = 0
            WHERE gis.SeasonId = %s
              {after}
            ORDER BY gis.SortOrder ASC, gi.Id ASC
            OFFSET 0 ROWS FETCH NEXT %s ROWS ONLY
            """,
            tuple(params),
        )
        return cursor.fetchall()

    def list_admin(self, season_id, status: str, caption_like: str, after_sort, after_id, limit: int) -> list[dict]:
        cursor = self.db.cursor()
        status = status if status in {"draft", "published"} else "all"
        searching = 1 if caption_like else 0
        like = caption_like or "%"
        if season_id:
            after = ""
            params: list = [season_id, status, status, status, searching, like]
            if after_sort is not None:
                after = "AND (gis.SortOrder > %s OR (gis.SortOrder = %s AND gi.Id > %s))"
                params.extend([after_sort, after_sort, after_id])
            params.append(limit)
            cursor.execute(
                f"""
                SELECT gi.Id, gi.SourceType, gi.MediaType, gi.Url, gi.StorageKey, gi.Width, gi.Height,
                       gi.Caption, gi.AltText, gi.TakenOn, gi.IsPublished, gis.SortOrder, gis.IsCover
                FROM tblGalleryItems gi
                JOIN tblGalleryItemSeasons gis ON gis.ItemId = gi.Id AND gis.SeasonId = %s
                WHERE gi.Void = 0
                  AND (%s = 'all' OR (%s = 'published' AND gi.IsPublished = 1) OR (%s = 'draft' AND gi.IsPublished = 0))
                  AND (%s = 0 OR gi.Caption LIKE %s)
                  {after}
                ORDER BY gis.SortOrder ASC, gi.Id ASC
                OFFSET 0 ROWS FETCH NEXT %s ROWS ONLY
                """,
                tuple(params),
            )
            return cursor.fetchall()
        after = ""
        params = [status, status, status, searching, like]
        if after_id is not None:
            after = "AND gi.Id < %s"
            params.append(after_id)
        params.append(limit)
        cursor.execute(
            f"""
            SELECT gi.Id, gi.SourceType, gi.MediaType, gi.Url, gi.StorageKey, gi.Width, gi.Height,
                   gi.Caption, gi.AltText, gi.TakenOn, gi.IsPublished,
                   CAST(0 AS INT) AS SortOrder, CAST(0 AS BIT) AS IsCover
            FROM tblGalleryItems gi
            WHERE gi.Void = 0
              AND (%s = 'all' OR (%s = 'published' AND gi.IsPublished = 1) OR (%s = 'draft' AND gi.IsPublished = 0))
              AND (%s = 0 OR gi.Caption LIKE %s)
              {after}
            ORDER BY gi.Id DESC
            OFFSET 0 ROWS FETCH NEXT %s ROWS ONLY
            """,
            tuple(params),
        )
        return cursor.fetchall()

    def public_seasons(self) -> list[dict]:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT s.Id, s.Name,
                   (
                       SELECT COUNT(*)
                       FROM tblGalleryItemSeasons gis
                       JOIN tblGalleryItems gi ON gi.Id = gis.ItemId AND gi.Void = 0 AND gi.IsPublished = 1
                       WHERE gis.SeasonId = s.Id
                   ) AS PhotoCount,
                   cover.Id AS CoverId, cover.SourceType AS CoverSource, cover.MediaType AS CoverMedia,
                   cover.Url AS CoverUrl, cover.StorageKey AS CoverKey,
                   cover.Width AS CoverWidth, cover.Height AS CoverHeight,
                   cover.Caption AS CoverCaption, cover.AltText AS CoverAlt, cover.TakenOn AS CoverTakenOn,
                   cover.IsCover AS CoverIsCover, cover.SortOrder AS CoverSort
            FROM tblSeasons s
            OUTER APPLY (
                SELECT TOP (1) gi.Id, gi.SourceType, gi.MediaType, gi.Url, gi.StorageKey, gi.Width, gi.Height,
                       gi.Caption, gi.AltText, gi.TakenOn, gis.IsCover, gis.SortOrder
                FROM tblGalleryItemSeasons gis
                JOIN tblGalleryItems gi ON gi.Id = gis.ItemId AND gi.Void = 0 AND gi.IsPublished = 1
                WHERE gis.SeasonId = s.Id
                ORDER BY gis.IsCover DESC, gis.SortOrder ASC, gi.Id ASC
            ) cover
            WHERE s.Void = 0
              AND EXISTS (
                  SELECT 1
                  FROM tblGalleryItemSeasons published
                  JOIN tblGalleryItems item ON item.Id = published.ItemId AND item.Void = 0 AND item.IsPublished = 1
                  WHERE published.SeasonId = s.Id
              )
            ORDER BY s.StartDate DESC, s.Id DESC
            """
        )
        return cursor.fetchall()

    def item_ids_for_season(self, season_id: int) -> list[int]:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT gis.ItemId
            FROM tblGalleryItemSeasons gis
            JOIN tblGalleryItems gi ON gi.Id = gis.ItemId AND gi.Void = 0
            WHERE gis.SeasonId = %s
            """,
            (season_id,),
        )
        return [int(row["ItemId"]) for row in cursor.fetchall()]

    def set_order(self, season_id: int, item_ids: list[int]) -> None:
        cursor = self.db.cursor()
        for index, item_id in enumerate(item_ids):
            cursor.execute(
                "UPDATE tblGalleryItemSeasons SET SortOrder = %s WHERE SeasonId = %s AND ItemId = %s",
                (index, season_id, item_id),
            )

    def set_cover(self, season_id: int, item_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT TOP (1) ItemId
            FROM tblGalleryItemSeasons
            WHERE SeasonId = %s AND ItemId = %s
            """,
            (season_id, item_id),
        )
        if cursor.fetchone() is None:
            return False
        cursor.execute(
            "UPDATE tblGalleryItemSeasons SET IsCover = 0 WHERE SeasonId = %s AND IsCover = 1",
            (season_id,),
        )
        cursor.execute(
            "UPDATE tblGalleryItemSeasons SET IsCover = 1 WHERE SeasonId = %s AND ItemId = %s",
            (season_id, item_id),
        )
        return True
