from uuid import UUID

from app.chat.manager import ChatManager
from app.workspaces.manager import WorkspaceManager
from database.session import get_custom_db_context_session


class EmbedModelService:
    async def get_workspace_and_owner(self, token: str):
        async with get_custom_db_context_session() as db:
            return await WorkspaceManager(db).get_workspace_by_embed_token(token)

    async def get_or_create_conversation(
        self,
        conversation_id: UUID | None,
        workspace_id: UUID,
        user_id: UUID,
    ) -> UUID:
        async with get_custom_db_context_session() as db:
            return await ChatManager(db).get_or_create_embed_conversation(
                conversation_id,
                workspace_id,
                user_id,
            )

    async def update_spend_from_message(self, workspace_id: UUID, message_id: UUID) -> None:
        async with get_custom_db_context_session() as db:
            cost, tokens = await ChatManager(db).get_message_usage(message_id)
            if cost > 0 or tokens > 0:
                await WorkspaceManager(db).increment_embed_spend(workspace_id, cost, tokens)

    async def auto_disable_embed(self, workspace_id: UUID) -> None:
        async with get_custom_db_context_session() as db:
            await WorkspaceManager(db).auto_disable_embed(workspace_id)
