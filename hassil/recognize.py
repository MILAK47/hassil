"""Methods for recognizing intents from text."""

import collections.abc
import itertools
import re
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .expression import (
    Expression,
    ListReference,
    RuleReference,
    Sentence,
    Sequence,
    SequenceType,
    TextChunk,
)
from .intents import (
    Intent,
    IntentData,
    Intents,
    RangeSlotList,
    SlotList,
    TextSlotList,
    WildcardSlotList,
)
from .util import normalize_text, normalize_whitespace

NUMBER_START = re.compile(r"^(\s*-?[0-9]+)")
PUNCTUATION = re.compile(r"[.。,，?¿？؟!！;；:：]+")
WHITESPACE = re.compile(r"\s+")

MISSING_ENTITY = "<missing>"


class HassilError(Exception):
    """Base class for hassil errors"""


class MissingListError(HassilError):
    """Error when a {slot_list} is missing."""


class MissingRuleError(HassilError):
    """Error when an <expansion_rule> is missing."""


@dataclass
class MatchEntity:
    """Named entity that has been matched from a {slot_list}"""

    name: str
    """Name of the entity."""

    value: Any
    """Value of the entity."""

    text: str
    """Original value text."""

    is_wildcard: bool = False
    """True if entity is a wildcard."""

    is_wildcard_open: bool = True
    """While True, wildcard can continue matching."""

    @property
    def text_clean(self) -> str:
        """Trimmed text with punctuation removed."""
        return PUNCTUATION.sub("", self.text.strip())


@dataclass
class UnmatchedEntity(ABC):
    """Base class for unmatched entities."""

    name: str
    """Name of entity that should have matched."""


@dataclass
class UnmatchedTextEntity(UnmatchedEntity):
    """Text entity that should have matched."""

    text: str
    """Text that failed to match slot values."""

    is_open: bool = True
    """While True, entity can continue matching."""


@dataclass
class UnmatchedRangeEntity(UnmatchedEntity):
    """Range entity that should have matched."""

    value: int
    """Value of entity that was out of range."""


@dataclass
class MatchSettings:
    """Settings used in match_expression."""

    slot_lists: Dict[str, SlotList] = field(default_factory=dict)
    """Available slot lists mapped by name."""

    expansion_rules: Dict[str, Sentence] = field(default_factory=dict)
    """Available expansion rules mapped by name."""

    ignore_whitespace: bool = False
    """True if whitespace should be ignored during matching."""

    allow_unmatched_entities: bool = False


@dataclass
class MatchContext:
    """Context passed to match_expression."""

    text: str
    """Input text remaining to be processed."""

    entities: List[MatchEntity] = field(default_factory=list)
    """Entities that have been found in input text."""

    intent_context: Dict[str, Any] = field(default_factory=dict)
    """Context items from outside or acquired during matching."""

    is_start_of_word: bool = True
    """True if current text is the start of a word."""

    unmatched_entities: List[UnmatchedEntity] = field(default_factory=list)
    """Entities that failed to match (requires allow_unmatched_entities=True)."""

    close_wildcards: bool = False
    """True if open wildcards should be closed during init."""

    close_unmatched: bool = False
    """True if open unmatched entities should be closed during init."""

    def __post_init__(self):
        if self.close_wildcards:
            for entity in self.entities:
                entity.is_wildcard_open = False

        if self.close_unmatched:
            for unmatched_entity in self.unmatched_entities:
                if isinstance(unmatched_entity, UnmatchedTextEntity):
                    unmatched_entity.is_open = False

    @property
    def is_match(self) -> bool:
        """True if no text is left that isn't just whitespace or punctuation"""
        text = PUNCTUATION.sub("", self.text).strip()
        if text:
            return False

        # Wildcards cannot be empty
        for entity in self.entities:
            if entity.is_wildcard and (not entity.text.strip()):
                return False

        # Unmatched entities cannot be empty
        for unmatched_entity in self.unmatched_entities:
            if isinstance(unmatched_entity, UnmatchedTextEntity) and (
                not unmatched_entity.text.strip()
            ):
                return False

        return True

    def get_open_wildcard(self) -> Optional[MatchEntity]:
        """Get the last open wildcard or None."""
        if not self.entities:
            return None

        last_entity = self.entities[-1]
        if last_entity.is_wildcard and last_entity.is_wildcard_open:
            return last_entity

        return None

    def get_open_entity(self) -> Optional[UnmatchedTextEntity]:
        """Get the last open unmatched text entity or None."""
        if not self.unmatched_entities:
            return None

        last_entity = self.unmatched_entities[-1]
        if isinstance(last_entity, UnmatchedTextEntity) and last_entity.is_open:
            return last_entity

        return None


@dataclass
class RecognizeResult:
    """Result of recognition."""

    intent: Intent
    """Matched intent"""

    intent_data: IntentData
    """Matched intent data"""

    entities: Dict[str, MatchEntity] = field(default_factory=dict)
    """Matched entities mapped by name."""

    entities_list: List[MatchEntity] = field(default_factory=list)
    """Matched entities as a list (duplicates allowed)."""

    response: Optional[str] = None
    """Key for intent response."""

    context: Dict[str, Any] = field(default_factory=dict)
    """Context values acquired during matching."""

    unmatched_entities: Dict[str, UnmatchedEntity] = field(default_factory=dict)
    """Unmatched entities mapped by name."""

    unmatched_entities_list: List[UnmatchedEntity] = field(default_factory=list)
    """Unmatched entities as a list (duplicates allowed)."""


def recognize(
    text: str,
    intents: Intents,
    slot_lists: Optional[Dict[str, SlotList]] = None,
    expansion_rules: Optional[Dict[str, Sentence]] = None,
    skip_words: Optional[Iterable[str]] = None,
    intent_context: Optional[Dict[str, Any]] = None,
    default_response: Optional[str] = "default",
    allow_unmatched_entities: bool = False,
) -> Optional[RecognizeResult]:
    """Return the first match of input text/words against a collection of intents.

    text: Text to recognize
    intents: Compiled intents
    slot_lists: Pre-defined text lists, ranges, or wildcards
    expansion_rules: Named template snippets
    skip_words: Strings to ignore in text
    intent_context: Slot values to use when not found in text
    default_response: Response key to use if not set in intent
    allow_unmatched_entities: True if entity values outside slot lists are allowed (slower)

    Returns the first result.
    If allow_unmatched_entities is True, you should check for unmatched entities.
    """
    for result in recognize_all(
        text,
        intents,
        slot_lists=slot_lists,
        expansion_rules=expansion_rules,
        skip_words=skip_words,
        intent_context=intent_context,
        default_response=default_response,
        allow_unmatched_entities=allow_unmatched_entities,
    ):
        return result

    return None


def recognize_all(
    text: str,
    intents: Intents,
    slot_lists: Optional[Dict[str, SlotList]] = None,
    expansion_rules: Optional[Dict[str, Sentence]] = None,
    skip_words: Optional[Iterable[str]] = None,
    intent_context: Optional[Dict[str, Any]] = None,
    default_response: Optional[str] = "default",
    allow_unmatched_entities: bool = False,
) -> Iterable[RecognizeResult]:
    """Return all matches for input text/words against a collection of intents.

    text: Text to recognize
    intents: Compiled intents
    slot_lists: Pre-defined text lists, ranges, or wildcards
    expansion_rules: Named template snippets
    skip_words: Strings to ignore in text
    intent_context: Slot values to use when not found in text
    default_response: Response key to use if not set in intent
    allow_unmatched_entities: True if entity values outside slot lists are allowed (slower)

    Yields results as they're matched.
    If allow_unmatched_entities is True, you should check for unmatched entities.
    """
    text = normalize_text(text).strip()

    if skip_words is None:
        skip_words = intents.skip_words
    else:
        # Combine skip words
        skip_words = itertools.chain(skip_words, intents.skip_words)

    if skip_words:
        text = _remove_skip_words(text, skip_words, intents.settings.ignore_whitespace)

    if intents.settings.ignore_whitespace:
        text = WHITESPACE.sub("", text)
    else:
        # Artifical word boundary
        text += " "

    if slot_lists is None:
        slot_lists = intents.slot_lists
    else:
        # Combine with intents
        slot_lists = {**intents.slot_lists, **slot_lists}

    if slot_lists is None:
        slot_lists = {}

    if expansion_rules is None:
        expansion_rules = intents.expansion_rules
    else:
        # Combine rules
        expansion_rules = {**intents.expansion_rules, **expansion_rules}

    if intent_context is None:
        intent_context = {}

    settings = MatchSettings(
        slot_lists=slot_lists,
        expansion_rules=expansion_rules,
        ignore_whitespace=intents.settings.ignore_whitespace,
        allow_unmatched_entities=allow_unmatched_entities,
    )

    # Check sentence against each intent.
    # This should eventually be done in parallel.
    for intent in intents.intents.values():
        for intent_data in intent.data:
            if intent_context:
                # Skip sentence templates that can't possibly be matched due to
                # requires/excludes context.
                #
                # Additional context can be added during matching, so we can
                # only be sure about keys that exist right now.
                skip_data = False
                if intent_data.requires_context:
                    for (
                        required_key,
                        required_value,
                    ) in intent_data.requires_context.items():
                        if (required_value is None) or (
                            required_key not in intent_context
                        ):
                            # None is wildcard
                            continue

                        # Ensure value matches
                        actual_value = intent_context[required_key]
                        if isinstance(required_value, collections.abc.Collection):
                            if actual_value not in required_value:
                                skip_data = True
                                break
                        elif actual_value != required_value:
                            skip_data = True
                            break

                if skip_data:
                    continue

                if intent_data.excludes_context:
                    for (
                        excluded_key,
                        excluded_value,
                    ) in intent_data.requires_context.items():
                        if excluded_key not in intent_context:
                            continue

                        # Ensure value does not match
                        actual_value = intent_context[excluded_key]
                        if isinstance(excluded_value, collections.abc.Collection):
                            if actual_value in excluded_value:
                                skip_data = True
                                break
                        elif actual_value == excluded_value:
                            skip_data = True
                            break

                if skip_data:
                    continue

            local_settings = MatchSettings(
                slot_lists=settings.slot_lists,
                expansion_rules={
                    **settings.expansion_rules,
                    **intent_data.expansion_rules,
                },
                ignore_whitespace=settings.ignore_whitespace,
                allow_unmatched_entities=allow_unmatched_entities,
            )

            # Check each sentence template
            for intent_sentence in intent_data.sentences:
                # Create initial context
                match_context = MatchContext(
                    text=text,
                    intent_context=intent_context,
                )
                maybe_match_contexts = match_expression(
                    local_settings, match_context, intent_sentence
                )
                for maybe_match_context in maybe_match_contexts:
                    # Close any open wildcards or unmatched entities
                    final_text = maybe_match_context.text.strip()
                    if final_text:
                        if unmatched_entity := maybe_match_context.get_open_entity():
                            # Consume the rest of the text (unmatched entity)
                            unmatched_entity.text += final_text
                            unmatched_entity.is_open = False
                            maybe_match_context.text = ""
                        elif wildcard := maybe_match_context.get_open_wildcard():
                            # Consume the rest of the text (wildcard)
                            wildcard.text += final_text
                            wildcard.value = wildcard.text
                            wildcard.is_wildcard_open = False
                            maybe_match_context.text = ""

                    if not maybe_match_context.is_match:
                        # Incomplete match with text still left at the end
                        continue

                    skip_match = False

                    # Verify excluded context
                    if intent_data.excludes_context:
                        for (
                            context_key,
                            context_value,
                        ) in intent_data.excludes_context.items():
                            actual_value = maybe_match_context.intent_context.get(
                                context_key
                            )
                            if actual_value == context_value:
                                # Exact match to context value
                                skip_match = True
                                break

                            if (
                                isinstance(context_value, collections.abc.Collection)
                                and not isinstance(context_value, str)
                                and (actual_value in context_value)
                            ):
                                # Actual value was in context value list
                                skip_match = True
                                break

                    # Verify required context
                    if (not skip_match) and intent_data.requires_context:
                        for (
                            context_key,
                            context_value,
                        ) in intent_data.requires_context.items():
                            actual_value = maybe_match_context.intent_context.get(
                                context_key
                            )

                            if allow_unmatched_entities and (actual_value is None):
                                # Look in unmatched entities
                                for (
                                    unmatched_context_entity
                                ) in maybe_match_context.unmatched_entities:
                                    if (
                                        unmatched_context_entity.name == context_key
                                    ) and isinstance(
                                        unmatched_context_entity, UnmatchedTextEntity
                                    ):
                                        actual_value = unmatched_context_entity.text
                                        break

                            if (
                                actual_value == context_value
                                and context_value is not None
                            ):
                                # Exact match to context value, except when context value is required and not provided
                                continue

                            if (context_value is None) and (actual_value is not None):
                                # Any value matches, as long as it's set
                                continue

                            if (
                                isinstance(context_value, collections.abc.Collection)
                                and not isinstance(context_value, str)
                                and (actual_value in context_value)
                            ):
                                # Actual value was in context value list
                                continue

                            if allow_unmatched_entities:
                                # Create missing entity as unmatched
                                has_unmatched_entity = False
                                for (
                                    unmatched_context_entity
                                ) in maybe_match_context.unmatched_entities:
                                    if unmatched_context_entity.name == context_key:
                                        has_unmatched_entity = True
                                        break

                                if not has_unmatched_entity:
                                    maybe_match_context.unmatched_entities.append(
                                        UnmatchedTextEntity(
                                            name=context_key,
                                            text=MISSING_ENTITY,
                                            is_open=False,
                                        )
                                    )
                            else:
                                # Did not match required context
                                skip_match = True
                                break

                    if skip_match:
                        # Intent context did not match
                        continue

                    # Add fixed entities
                    entity_names = set(
                        entity.name for entity in maybe_match_context.entities
                    )
                    for slot_name, slot_value in intent_data.slots.items():
                        if slot_name not in entity_names:
                            maybe_match_context.entities.append(
                                MatchEntity(name=slot_name, value=slot_value, text="")
                            )

                    # Return each match
                    response = default_response
                    if intent_data.response is not None:
                        response = intent_data.response

                    yield RecognizeResult(
                        intent=intent,
                        intent_data=intent_data,
                        entities={
                            entity.name: entity
                            for entity in maybe_match_context.entities
                        },
                        entities_list=maybe_match_context.entities,
                        response=response,
                        context=maybe_match_context.intent_context,
                        unmatched_entities={
                            entity.name: entity
                            for entity in maybe_match_context.unmatched_entities
                        },
                        unmatched_entities_list=maybe_match_context.unmatched_entities,
                    )


def is_match(
    text: str,
    sentence: Sentence,
    slot_lists: Optional[Dict[str, SlotList]] = None,
    expansion_rules: Optional[Dict[str, Sentence]] = None,
    skip_words: Optional[Iterable[str]] = None,
    entities: Optional[Dict[str, Any]] = None,
    intent_context: Optional[Dict[str, Any]] = None,
    ignore_whitespace: bool = False,
    allow_unmatched_entities: bool = False,
) -> Optional[MatchContext]:
    """Return the first match of input text/words against a sentence expression."""
    text = normalize_text(text).strip()

    if skip_words:
        text = _remove_skip_words(text, skip_words, ignore_whitespace)

    if ignore_whitespace:
        text = WHITESPACE.sub("", text)
    else:
        # Artifical word boundary
        text += " "

    if slot_lists is None:
        slot_lists = {}

    if expansion_rules is None:
        expansion_rules = {}

    if intent_context is None:
        intent_context = {}

    settings = MatchSettings(
        slot_lists=slot_lists,
        expansion_rules=expansion_rules,
        ignore_whitespace=ignore_whitespace,
        allow_unmatched_entities=allow_unmatched_entities,
    )

    match_context = MatchContext(
        text=text,
        intent_context=intent_context,
    )

    for maybe_match_context in match_expression(settings, match_context, sentence):
        if maybe_match_context.is_match:
            return maybe_match_context

    return None


def _remove_skip_words(
    text: str, skip_words: Iterable[str], ignore_whitespace: bool
) -> str:
    """Remove skip words from text."""

    # It's critical that skip words are processed longest first, since they may
    # share prefixes.
    for skip_word in sorted(skip_words, key=len, reverse=True):
        skip_word = normalize_text(skip_word)
        if ignore_whitespace:
            text = text.replace(skip_word, "")
        else:
            # Use word boundaries
            text = re.sub(rf"\b{re.escape(skip_word)}\b", "", text)

    if not ignore_whitespace:
        text = normalize_whitespace(text)
        text = text.strip()

    return text


def match_expression(
    settings: MatchSettings, context: MatchContext, expression: Expression
) -> Iterable[MatchContext]:
    """Yield matching contexts for an expression"""
    if isinstance(expression, TextChunk):
        chunk: TextChunk = expression

        if settings.ignore_whitespace:
            # Remove all whitespace
            chunk_text = WHITESPACE.sub("", chunk.text)
            context_text = WHITESPACE.sub("", context.text)
        else:
            # Keep whitespace
            chunk_text = chunk.text
            context_text = context.text

            if context.is_start_of_word:
                # Ignore extra whitespace at the beginning of chunk and text
                # since we know we're at the start of a word.
                chunk_text = chunk_text.lstrip()
                context_text = context_text.lstrip()

        # True if remaining text to be matched is empty or whitespace.
        #
        # If so, we can't say this is a successful match yet because the
        # sentence template may have remaining non-optional expressions.
        #
        # So we have to continue matching, skipping over empty or whitespace
        # chunks until the template is exhausted.
        is_context_text_empty = len(context_text.strip()) == 0

        if chunk.is_empty:
            # Skip empty chunk (NOT whitespace)
            yield context
        else:
            wildcard = context.get_open_wildcard()
            if (wildcard is not None) and (not wildcard.text.strip()):
                if not chunk_text.strip():
                    # Skip space
                    yield MatchContext(
                        text=context_text,
                        is_start_of_word=True,
                        # Copy over
                        entities=context.entities,
                        intent_context=context.intent_context,
                        unmatched_entities=context.unmatched_entities,
                    )
                    return

                # Wildcard cannot be empty
                start_idx = context_text.find(chunk_text)
                if start_idx < 0:
                    # Cannot possibly match
                    return

                if start_idx == 0:
                    # Possible degenerate case where the next word in the
                    # template duplicates.
                    start_idx = context_text.find(chunk_text, 1)
                    if start_idx < 0:
                        # Cannot possibly match
                        return

                while start_idx > 0:
                    wildcard_text = context_text[:start_idx]
                    yield from match_expression(
                        settings,
                        MatchContext(
                            text=context_text[start_idx:],
                            is_start_of_word=True,
                            entities=context.entities[:-1]
                            + [
                                MatchEntity(
                                    name=wildcard.name,
                                    text=wildcard_text,
                                    value=wildcard_text,
                                )
                            ],
                            # Copy over
                            intent_context=context.intent_context,
                            unmatched_entities=context.unmatched_entities,
                        ),
                        expression,
                    )
                    start_idx = context_text.find(chunk_text, start_idx + 1)

                # Do not continue with matching
                return

            if context_text.startswith(chunk_text):
                # Successful match for chunk
                context_text = context_text[len(chunk_text) :]
                is_chunk_word = bool(chunk_text) and (not chunk_text.isspace())
                yield MatchContext(
                    text=context_text,
                    # must use chunk.text because it hasn't been stripped
                    is_start_of_word=chunk.text.endswith(" "),
                    # Copy over
                    entities=context.entities,
                    intent_context=context.intent_context,
                    unmatched_entities=context.unmatched_entities,
                    #
                    close_wildcards=is_chunk_word,
                    close_unmatched=is_chunk_word,
                )
            elif is_context_text_empty and chunk_text.isspace():
                # No text left to match, so extra whitespace is OK to skip
                yield context
            else:
                # Remove punctuation and try again
                context_text = PUNCTUATION.sub("", context.text)
                context_starts_with = context_text.startswith(chunk_text)
                if (not context_starts_with) and context.is_start_of_word:
                    # Try stripping whitespace
                    context_text = context_text.lstrip()
                    context_starts_with = context_text.startswith(chunk_text)

                if context_starts_with:
                    context_text = context_text[len(chunk_text) :]
                    yield MatchContext(
                        text=context_text,
                        # Copy over
                        entities=context.entities,
                        intent_context=context.intent_context,
                        is_start_of_word=context.is_start_of_word,
                        unmatched_entities=context.unmatched_entities,
                    )
                elif wildcard is not None:
                    # Add to wildcard by skipping ahead in the text until we find
                    # the current chunk text.
                    skip_idx = context_text.find(chunk_text)
                    if skip_idx >= 0:
                        wildcard.text += context_text[:skip_idx]

                        # Wildcards cannot be empty
                        if wildcard.text:
                            wildcard.value = wildcard.text
                            yield MatchContext(
                                text=context.text[skip_idx + len(chunk_text) :],
                                # Copy over
                                entities=context.entities,
                                intent_context=context.intent_context,
                                is_start_of_word=True,
                                unmatched_entities=context.unmatched_entities,
                            )
                elif settings.allow_unmatched_entities and (
                    unmatched_entity := context.get_open_entity()
                ):
                    # Add to the most recent unmatched entity by skipping ahead in
                    # the text until we find the current chunk text.
                    skip_idx = context_text.find(chunk_text)
                    if skip_idx >= 0:
                        unmatched_entity.text += context_text[:skip_idx]

                        # Unmatched entities cannot be empty
                        if unmatched_entity.text:
                            yield MatchContext(
                                text=context.text[skip_idx + len(chunk_text) :],
                                # Copy over
                                entities=context.entities,
                                intent_context=context.intent_context,
                                is_start_of_word=True,
                                unmatched_entities=context.unmatched_entities,
                            )
                else:
                    # Match failed
                    pass
    elif isinstance(expression, Sequence):
        seq: Sequence = expression
        if seq.type == SequenceType.ALTERNATIVE:
            # Any may match (words | in | alternative)
            # NOTE: [optional] = (optional | )
            for item in seq.items:
                yield from match_expression(settings, context, item)

        elif seq.type == SequenceType.GROUP:
            if seq.items:
                # All must match (words in group)
                group_contexts = [context]
                for item in seq.items:
                    # Next step
                    group_contexts = [
                        item_context
                        for group_context in group_contexts
                        for item_context in match_expression(
                            settings, group_context, item
                        )
                    ]
                    if not group_contexts:
                        break

                for group_context in group_contexts:
                    yield group_context
        else:
            raise ValueError(f"Unexpected sequence type: {seq}")

    elif isinstance(expression, ListReference):
        # {list}
        list_ref: ListReference = expression
        if (not settings.slot_lists) or (list_ref.list_name not in settings.slot_lists):
            raise MissingListError(f"Missing slot list {{{list_ref.list_name}}}")

        slot_list = settings.slot_lists[list_ref.list_name]
        if isinstance(slot_list, TextSlotList):
            if context.text:
                text_list: TextSlotList = slot_list
                # Any value may match
                for slot_value in text_list.values:
                    value_contexts = match_expression(
                        settings,
                        MatchContext(
                            # Copy over
                            text=context.text,
                            entities=context.entities,
                            intent_context=context.intent_context,
                            is_start_of_word=context.is_start_of_word,
                            unmatched_entities=context.unmatched_entities,
                        ),
                        slot_value.text_in,
                    )

                    has_matches = False
                    for value_context in value_contexts:
                        has_matches = True
                        entities = context.entities + [
                            MatchEntity(
                                name=list_ref.slot_name,
                                value=slot_value.value_out,
                                text=context.text[: -len(value_context.text)]
                                if value_context.text
                                else context.text,
                            )
                        ]

                        if slot_value.context:
                            # Merge context from matched list value
                            yield MatchContext(
                                entities=entities,
                                intent_context={
                                    **context.intent_context,
                                    **slot_value.context,
                                },
                                # Copy over
                                text=value_context.text,
                                is_start_of_word=context.is_start_of_word,
                                unmatched_entities=context.unmatched_entities,
                            )
                        else:
                            yield MatchContext(
                                entities=entities,
                                # Copy over
                                text=value_context.text,
                                intent_context=value_context.intent_context,
                                is_start_of_word=context.is_start_of_word,
                                unmatched_entities=context.unmatched_entities,
                            )

                    if (not has_matches) and settings.allow_unmatched_entities:
                        # Report mismatch
                        yield MatchContext(
                            # Copy over
                            text=context.text,
                            entities=context.entities,
                            intent_context=context.intent_context,
                            is_start_of_word=context.is_start_of_word,
                            #
                            unmatched_entities=context.unmatched_entities
                            + [UnmatchedTextEntity(name=list_ref.slot_name, text="")],
                            close_wildcards=True,
                        )

        elif isinstance(slot_list, RangeSlotList):
            if context.text:
                # List that represents a number range.
                # Numbers must currently be digits ("1" not "one").
                range_list: RangeSlotList = slot_list
                number_match = NUMBER_START.match(context.text)
                if number_match is not None:
                    number_text = number_match[1]
                    word_number = int(number_text)
                    if range_list.step == 1:
                        # Unit step
                        in_range = range_list.start <= word_number <= range_list.stop
                    else:
                        # Non-unit step
                        in_range = word_number in range(
                            range_list.start, range_list.stop + 1, range_list.step
                        )

                    if in_range:
                        entities = context.entities + [
                            MatchEntity(
                                name=list_ref.slot_name,
                                value=word_number,
                                text=context.text.split()[0],
                            )
                        ]

                        yield MatchContext(
                            text=context.text[len(number_text) :],
                            entities=entities,
                            # Copy over
                            intent_context=context.intent_context,
                            is_start_of_word=context.is_start_of_word,
                            unmatched_entities=context.unmatched_entities,
                        )
                    elif settings.allow_unmatched_entities:
                        # Report out of range
                        yield MatchContext(
                            # Copy over
                            text=context.text[len(number_text) :],
                            entities=context.entities,
                            intent_context=context.intent_context,
                            is_start_of_word=context.is_start_of_word,
                            #
                            unmatched_entities=context.unmatched_entities
                            + [
                                UnmatchedRangeEntity(
                                    name=list_ref.slot_name, value=word_number
                                )
                            ],
                        )
                elif settings.allow_unmatched_entities:
                    # Report not a number
                    yield MatchContext(
                        # Copy over
                        text=context.text,
                        entities=context.entities,
                        intent_context=context.intent_context,
                        is_start_of_word=context.is_start_of_word,
                        #
                        unmatched_entities=context.unmatched_entities
                        + [UnmatchedTextEntity(name=list_ref.slot_name, text="")],
                        close_wildcards=True,
                    )
        elif isinstance(slot_list, WildcardSlotList):
            if context.text:
                # Start wildcard entities
                yield MatchContext(
                    # Copy over
                    text=context.text,
                    intent_context=context.intent_context,
                    is_start_of_word=context.is_start_of_word,
                    unmatched_entities=context.unmatched_entities,
                    #
                    entities=context.entities
                    + [
                        MatchEntity(
                            name=list_ref.slot_name, value="", text="", is_wildcard=True
                        )
                    ],
                    close_unmatched=True,
                )
        else:
            raise ValueError(f"Unexpected slot list type: {slot_list}")

    elif isinstance(expression, RuleReference):
        # <rule>
        rule_ref: RuleReference = expression
        if (not settings.expansion_rules) or (
            rule_ref.rule_name not in settings.expansion_rules
        ):
            raise MissingRuleError(f"Missing expansion rule <{rule_ref.rule_name}>")

        yield from match_expression(
            settings, context, settings.expansion_rules[rule_ref.rule_name]
        )
    else:
        raise ValueError(f"Unexpected expression: {expression}")


def _normalize_whitespace(text: str) -> str:
    return WHITESPACE.sub(" ", text)
