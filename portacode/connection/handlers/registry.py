"""Command registry for managing handler dispatch."""

import asyncio
import logging
from typing import Dict, Type, Any, Optional, List, TYPE_CHECKING
from portacode.utils.ntp_clock import ntp_clock

if TYPE_CHECKING:
    from ..multiplex import Channel
    from .base import BaseHandler

logger = logging.getLogger(__name__)


class CommandRegistry:
    """Registry for managing command handlers."""
    
    def __init__(self, control_channel: "Channel", context: Dict[str, Any]):
        """Initialize the command registry.
        
        Args:
            control_channel: The control channel for handlers
            context: Shared context for handlers
        """
        self.control_channel = control_channel
        self.context = context
        self._handlers: Dict[str, "BaseHandler"] = {}
        self._inflight_dispatches: Dict[str, asyncio.Task[bool]] = {}
        
    def register(self, handler_class: Type["BaseHandler"]) -> None:
        """Register a command handler.
        
        Args:
            handler_class: The handler class to register
        """
        handler_instance = handler_class(self.control_channel, self.context)
        command_name = handler_instance.command_name
        
        if command_name in self._handlers:
            logger.warning("Overriding existing handler for command: %s", command_name)
        
        self._handlers[command_name] = handler_instance
        logger.debug("Registered handler for command: %s", command_name)
        
    def unregister(self, command_name: str) -> None:
        """Unregister a command handler.
        
        Args:
            command_name: The command name to unregister
        """
        if command_name in self._handlers:
            del self._handlers[command_name]
            logger.debug("Unregistered handler for command: %s", command_name)
        else:
            logger.warning("Attempted to unregister non-existent handler: %s", command_name)
    
    def get_handler(self, command_name: str) -> Optional["BaseHandler"]:
        """Get a handler by command name.
        
        Args:
            command_name: The command name
            
        Returns:
            The handler instance or None if not found
        """
        return self._handlers.get(command_name)
    
    def list_commands(self) -> List[str]:
        """List all registered command names.
        
        Returns:
            List of command names
        """
        return list(self._handlers.keys())
    
    async def dispatch(self, command_name: str, message: Dict[str, Any], reply_channel: Optional[str] = None) -> bool:
        """Dispatch a command to its handler.

        Args:
            command_name: The command name
            message: The command message
            reply_channel: Optional reply channel

        Returns:
            True if handler was found and executed, False otherwise
        """
        logger.info("registry: Dispatching command '%s' with reply_channel=%s", command_name, reply_channel)

        # Add handler_receive timestamp if trace present
        if "trace" in message and "request_id" in message:
            handler_receive_time = ntp_clock.now_ms()
            if handler_receive_time is not None:
                message["trace"]["handler_receive"] = handler_receive_time
                # Update ping to show total time from client_send
                if "client_send" in message["trace"]:
                    message["trace"]["ping"] = handler_receive_time - message["trace"]["client_send"]
                logger.info(f"🎯 Handler received traced message: {message['request_id']}")

        handler = self.get_handler(command_name)
        if handler is None:
            logger.warning("registry: No handler found for command: %s", command_name)
            return False

        # System information is requested by periodic refreshes, dashboard
        # joins, and explicit probes. All of those sources must share one
        # collection; Proxmox discovery is too expensive to run concurrently.
        if command_name == "system_info":
            existing = self._inflight_dispatches.get(command_name)
            if existing is not None and not existing.done():
                logger.info("registry: Coalesced concurrent system_info request")
                return True
            task = asyncio.create_task(
                self._dispatch_handler(command_name, handler, message, reply_channel)
            )
            self._inflight_dispatches[command_name] = task

            def _clear_inflight(completed: asyncio.Task[bool]) -> None:
                if self._inflight_dispatches.get(command_name) is completed:
                    self._inflight_dispatches.pop(command_name, None)

            task.add_done_callback(_clear_inflight)
            return True

        return await self._dispatch_handler(command_name, handler, message, reply_channel)

    async def _dispatch_handler(
        self,
        command_name: str,
        handler: "BaseHandler",
        message: Dict[str, Any],
        reply_channel: Optional[str],
    ) -> bool:
        try:
            await handler.handle(message, reply_channel)
            logger.info("registry: Successfully dispatched command '%s'", command_name)
            return True
        except Exception as exc:
            logger.exception("registry: Error dispatching command %s: %s", command_name, exc)
            # Send session-aware error response
            await self._send_session_aware_error(
                str(exc),
                reply_channel,
                message.get("project_id"),
                request_id=message.get("request_id"),
            )
            return False
    
    async def _send_session_aware_error(
        self,
        message: str,
        reply_channel: Optional[str] = None,
        project_id: str = None,
        request_id: Optional[str] = None,
    ) -> None:
        """Send an error response with client session awareness."""
        error_payload = {"event": "error", "message": message}
        if request_id:
            error_payload["request_id"] = request_id
        
        # Get client session manager from context
        client_session_manager = self.context.get("client_session_manager")
        
        if client_session_manager and client_session_manager.has_interested_clients():
            # Get target sessions
            target_sessions = client_session_manager.get_target_sessions(project_id)
            if not target_sessions:
                logger.debug("registry: No target sessions found, skipping error send")
                return
            
            # Add session targeting information
            error_payload["client_sessions"] = target_sessions
            
            # Add backward compatibility reply_channel (first session if not provided)
            if not reply_channel:
                reply_channel = client_session_manager.get_reply_channel_for_compatibility()
            if reply_channel:
                error_payload["reply_channel"] = reply_channel
            
            logger.debug("registry: Sending error to %d client sessions: %s", 
                        len(target_sessions), target_sessions)
        else:
            # Fallback to original behavior
            if reply_channel:
                error_payload["reply_channel"] = reply_channel
        
        await self.control_channel.send(error_payload)

    def update_context(self, context: Dict[str, Any]) -> None:
        """Update the shared context for all handlers.
        
        Args:
            context: New context dict
        """
        self.context.update(context)
        
        # Update context for all existing handlers
        for handler in self._handlers.values():
            handler.context = self.context 
