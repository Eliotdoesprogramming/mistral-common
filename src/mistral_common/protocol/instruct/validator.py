import re
from enum import Enum
from typing import Generic, List

from jsonschema import Draft7Validator, SchemaError

from mistral_common.exceptions import (
    InvalidAssistantMessageException,
    InvalidFunctionCallException,
    InvalidMessageStructureException,
    InvalidRequestException,
    InvalidSystemPromptException,
    InvalidToolException,
    InvalidToolMessageException,
    InvalidToolSchemaException,
)
from mistral_common.protocol.instruct.messages import (
    UATS,
    AssistantMessage,
    AssistantMessageType,
    FinetuningAssistantMessage,
    Roles,
    SystemMessageType,
    ToolMessageType,
    UserMessageType,
)
from mistral_common.protocol.instruct.request import ChatCompletionRequest
from mistral_common.protocol.instruct.tool_calls import (
    Function,
    FunctionCall,
    Tool,
    ToolCall,
)


class ValidationMode(Enum):
    serving = "serving"
    finetuning = "finetuning"
    test = "test"


class MistralRequestValidator(Generic[UserMessageType, AssistantMessageType, ToolMessageType, SystemMessageType]):
    def __init__(self, mode: ValidationMode = ValidationMode.test):
        self._mode = mode

    def validate_messages(self, messages: List[UATS]) -> None:
        """
        Validates the list of messages
        """
        self._validate_message_list_structure(messages)
        self._validate_message_list_content(messages)

    def validate_request(self, request: ChatCompletionRequest) -> ChatCompletionRequest[UATS]:
        """
        Validates the request
        """

        if self._mode == ValidationMode.serving:
            if request.model is None:
                raise InvalidRequestException("Model name parameter is required for serving mode")

        # Validate the messages
        self.validate_messages(request.messages)

        # Validate the tools
        self._validate_tools(request.tools or [])

        return request

    def _validate_function(self, function: Function) -> None:
        """
        Checks:
        - That the function schema is valid
        """
        try:
            Draft7Validator.check_schema(function.parameters)
        except SchemaError as e:
            raise InvalidToolSchemaException(f"Invalid tool schema: {e.message}")

        if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", function.name):
            raise InvalidToolException(
                f"Function name was {function.name} but must be a-z, A-Z, 0-9, "
                "or contain underscores and dashes, with a maximum length of 64."
            )

    def _validate_tools(self, tools: List[Tool]) -> None:
        """
        Checks:
        - That the tool schemas are valid
        """

        for tool in tools:
            self._validate_function(tool.function)

    def _validate_user_message(self, message: UserMessageType) -> None:
        pass

    def _validate_tool_message(self, message: ToolMessageType) -> None:
        """
        Checks:
        - The tool name is valid
        """
        if message.name is not None:
            if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", message.name):
                raise InvalidToolMessageException(
                    f"Function name was {message.name} but must be a-z, A-Z, 0-9, "
                    "or contain underscores and dashes, with a maximum length of 64."
                )

    def _validate_system_message(self, message: SystemMessageType) -> None:
        """
        Checks:
        - That the system prompt has content
        """
        if message.content is None:
            raise InvalidSystemPromptException("System prompt must have content")

    def _validate_function_call(self, function_call: FunctionCall) -> None:
        """
        Checks:
        - That the function call has a valid name
        """
        if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", function_call.name):
            raise InvalidFunctionCallException(
                f"Function name was {function_call.name} but must be a-z, A-Z, 0-9, "
                "or contain underscores and dashes, with a maximum length of 64."
            )

    def _validate_tool_call(self, tool_call: ToolCall, is_last_message: bool) -> None:
        """
        Checks:
        - That the tool call has a valid function
        """

        self._validate_function_call(tool_call.function)

    def _validate_assistant_message(self, message: AssistantMessageType, is_last_message: bool = False) -> None:
        """
        Checks:
        - That the assistant message has either text or tool_calls, but not both
        - That the tool calls are valid
        """

        # Validate that the message has either text or tool_calls
        # but not both and not neither.
        if bool(message.content) == bool(message.tool_calls):
            raise InvalidAssistantMessageException(
                "Assistant message must have either content or tool_calls, but not both."
            )

        # If we have tool calls, validate them
        if message.tool_calls is not None:
            # Validate that the tool calls are valid
            for tool_call in message.tool_calls:
                self._validate_tool_call(tool_call, is_last_message=is_last_message)

        if self._mode == ValidationMode.finetuning and isinstance(message, FinetuningAssistantMessage):
            if message.weight is not None and message.weight not in [0, 1]:
                raise InvalidAssistantMessageException("Assistant message weight must be either 0 or 1")

        if message.prefix:
            if not is_last_message:
                raise InvalidAssistantMessageException("Assistant message with prefix True must be last message")
            # note : we already validate that assistant messsage has content 3 lines up.

    def _validate_tool_calls_followed_by_tool_messages(self, messages: List[UATS]) -> None:
        """
        Checks:
        - That the number of tool calls and tool messages are the same
        - That the tool calls are followed by tool messages
        """
        prev_role = None
        expected_tool_messages = 0
        for message in messages:
            if prev_role is None:
                prev_role = message.role
                continue

            if message.role == Roles.tool:
                expected_tool_messages -= 1
            elif message.role == Roles.assistant:
                # if we have an assistant message and we have not received all the function calls
                # we need to raise an exception
                if expected_tool_messages != 0:
                    raise InvalidMessageStructureException("Not the same number of function calls and responses")

                if message.tool_calls is not None:
                    # Validate that the number of function calls and responses are the same
                    expected_tool_messages = len(message.tool_calls)

            prev_role = message.role

        if expected_tool_messages != 0 and self._mode == ValidationMode.serving:
            raise InvalidMessageStructureException("Not the same number of function calls and responses")
        elif expected_tool_messages not in [0, 1] and self._mode == ValidationMode.finetuning:
            # if last assistant message has no tool calls, then same number of tool calls and messages => 0
            # if last assistant message has a tool call we have one more tool call => 1
            raise InvalidMessageStructureException("Too many function calls and too few responses")

    def _validate_message_order(self, messages: List[UATS]) -> None:
        """
        Validates the order of the messages, for example user -> assistant -> user -> assistant -> ...
        """
        previous_role = None
        for message in messages:
            current_role = message.role

            if previous_role is not None:
                if previous_role == Roles.system:
                    expected_roles = {Roles.user, Roles.assistant, Roles.system}
                elif previous_role == Roles.user:
                    expected_roles = {Roles.assistant, Roles.system, Roles.user}
                elif previous_role == Roles.assistant:
                    expected_roles = {Roles.assistant, Roles.user, Roles.tool}
                elif previous_role == Roles.tool:
                    expected_roles = {Roles.assistant, Roles.tool}

                if current_role not in expected_roles:
                    raise InvalidMessageStructureException(
                        f"Unexpected role '{current_role}' after role '{previous_role}'"
                    )

            previous_role = current_role

    def _validate_last_message(self, message: UATS) -> None:
        # The last message must be a user or tool message in serving mode or an assistant message in finetuning mode
        last_message_role = message.role
        if self._mode == ValidationMode.finetuning:
            if last_message_role != Roles.assistant:
                raise InvalidMessageStructureException(
                    f"Expected last role Assistant for finetuning but got {last_message_role}"
                )
        else:
            bad_assistant = isinstance(message, AssistantMessage) and not message.prefix
            bad_role = message.role not in {Roles.user, Roles.tool}
            if bad_assistant and bad_role:
                raise InvalidMessageStructureException(
                    f"Expected last role User or Tool (or Assistant with prefix True) for serving"
                    f" but got {last_message_role}"
                )

    def _validate_message_list_structure(self, messages: List[UATS]) -> None:
        """
        Validates the structure of the list of messages

        For example the messages must be in the correct order of user/assistant/tool
        """

        if len(messages) == 0:
            raise InvalidMessageStructureException("Conversation must have at least one message")

        # If we have one message it must be a user or a system message
        if len(messages) == 1:
            if messages[0].role not in {Roles.user, Roles.system}:
                raise InvalidMessageStructureException("Conversation must start with a user message or system message")

        # Always check the last message if in fine-tuning mode
        if self._mode == ValidationMode.finetuning or len(messages) > 1:
            self._validate_last_message(messages[-1])

        self._validate_message_order(messages)
        self._validate_tool_calls_followed_by_tool_messages(messages)

    def _validate_message_list_content(self, messages: List[UATS]) -> None:
        """
        Validates the content of the messages
        """

        for idx, message in enumerate(messages):
            if message.role == Roles.user:
                self._validate_user_message(message)
            elif message.role == Roles.assistant:
                self._validate_assistant_message(message, is_last_message=idx == len(messages) - 1)
            elif message.role == Roles.tool:
                self._validate_tool_message(message)
            elif message.role == Roles.system:
                self._validate_system_message(message)
            else:
                raise InvalidRequestException(f"Unsupported message type {type(message)}")


class MistralRequestValidatorV3(MistralRequestValidator):
    def _validate_tool_message(self, message: ToolMessageType) -> None:
        """
        Checks:
        - The tool name is valid
        - Tool call id is valid
        """
        if message.name is not None:
            if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", message.name):
                raise InvalidToolMessageException(
                    f"Function name was {message.name} but must be a-z, A-Z, 0-9, "
                    "or contain underscores and dashes, with a maximum length of 64."
                )

        if message.tool_call_id is None:
            raise InvalidRequestException("Tool call id has to be defined.")

        if not re.match(r"^[a-zA-Z0-9]{9}$", message.tool_call_id):
            raise InvalidToolMessageException(
                f"Tool call id was {message.tool_call_id} but must be a-z, A-Z, 0-9, with a length of 9."
            )

    def _validate_tool_call(self, tool_call: ToolCall, is_last_message: bool) -> None:
        """
        Validate that the tool call has a valid ID
        """
        if tool_call.id != "null":
            if not re.match(r"^[a-zA-Z0-9]{9}$", tool_call.id):
                raise InvalidFunctionCallException(
                    f"Tool call id was {tool_call.id} but must be a-z, A-Z, 0-9, with a length of 9."
                )
        if self._mode == ValidationMode.finetuning and not is_last_message and tool_call.id == "null":
            err_message = "Tool call id of assistant message that is not last has to be defined in finetuning mode."
            raise InvalidFunctionCallException(err_message)

        if self._mode == ValidationMode.serving and tool_call.id == "null":
            raise InvalidFunctionCallException("Tool call id has to be defined in serving mode.")

        self._validate_function_call(tool_call.function)

    def _validate_last_message(self, message: UATS) -> None:
        super()._validate_last_message(message)

        if self._mode == ValidationMode.finetuning:
            # in finetuning mode it has to be an assistant message
            # as checked by parent `_validate_last_message`
            if message.tool_calls is not None:
                for tool_call in message.tool_calls:
                    self._validate_tool_call(tool_call, is_last_message=True)
