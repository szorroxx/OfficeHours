from pydantic import Field
from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig

class TigerDataQueryConfig(FunctionBaseConfig, name="tiger_data_query"):
    """Config for querying assignment/schedule data from Tiger Data."""
    connection_string: str = Field(description="Postgres connection string for Tiger Cloud")

@register_function(config_type=TigerDataQueryConfig)
async def tiger_data_query_function(config: TigerDataQueryConfig, builder: Builder):
    import asyncpg

    async def _query(student_id: str) -> str:
        conn = await asyncpg.connect(config.connection_string)
        try:
            rows = await conn.fetch(
                """
                SELECT *
                FROM assignments
                ORDER BY due_at ASC
                LIMIT 20
                """,
                # student_id,
            )
        finally:
            await conn.close()

        if not rows:
            return "No upcoming assignments found."

        lines = [
            f"{r['course_code']}: {r['title']} — due {r['due_at']}"
            for r in rows
        ]
        return "\n".join(lines)

    yield FunctionInfo.from_fn(
        _query,
        description="Fetches a student's upcoming assignments and due dates from Tiger Data given a student_id.",
    )
