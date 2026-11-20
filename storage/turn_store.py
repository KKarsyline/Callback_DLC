class SQLiteTurnStore:
    async def completed_assistant_path(self, conv_id, channel_id, leaf_id):
        """The most recent six completed, visible replies on this exact branch."""
        if not self._conn or not leaf_id:
            return []
        # Child IDs carry branch identity; use the existing conversation run index
        # to distinguish completed assistants from interrupted partial messages.
        sql = """
            WITH RECURSIVE path(id, parent_id, rid) AS (
                SELECT id,parent_message_id,rowid FROM messages
                 WHERE id=? AND conversation_id=? AND channel_id=?
                UNION
                SELECT m.id,m.parent_message_id,m.rowid FROM messages m JOIN path p ON m.id=p.parent_id
                 WHERE m.conversation_id=? AND m.channel_id=?
            ), runs AS MATERIALIZED (
                SELECT assistant_message_id AS id, MAX(status='completed') AS done
                FROM assistant_runs WHERE conversation_id=? GROUP BY assistant_message_id
            )
            SELECT m.id FROM path p JOIN messages m ON m.id=p.id
              LEFT JOIN runs r ON r.id=m.id
             WHERE m.role='assistant' AND COALESCE(r.done,1)=1 AND COALESCE(m.is_proactive,0)=0
               AND CASE WHEN NOT json_valid(m.content) THEN length(trim(m.content))>0
                        WHEN json_type(m.content)='text' THEN length(trim(json_extract(m.content,'$')))>0
                        WHEN json_type(m.content)='array' THEN EXISTS (
                          SELECT 1 FROM json_each(m.content) b
                          WHERE b.type='object' AND json_extract(b.value,'$.type')='text'
                            AND length(trim(json_extract(b.value,'$.text')))>0)
                        ELSE 0 END
             ORDER BY p.rid DESC LIMIT 6"""
        async with self._conn.execute(sql,(leaf_id,conv_id,channel_id,conv_id,channel_id,conv_id)) as cur:
            return [r[0] for r in reversed(await cur.fetchall())]
