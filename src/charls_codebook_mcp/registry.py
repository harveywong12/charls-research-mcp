"""Tool profiles and strict native JSON validation shared by server and clients."""
import inspect
import types
from typing import get_args, get_origin, get_type_hints, Union

LEGACY_TOOLS = ('get_document_info', 'search_codebook', 'get_variable', 'read_source')
RESEARCH_TOOLS = (
    'get_research_overview', 'list_indicator_topics', 'list_datasets',
    'list_dataset_variables', 'search_data_variables', 'get_data_variable',
    'find_indicator_across_years', 'search_all_codebooks', 'read_codebook_page',
    'list_research_documents', 'get_variable_evidence', 'compare_variables',
    'assess_research_feasibility',
)
PROFILES = ('research', 'compat', 'legacy')


def tool_names(profile):
    if profile not in PROFILES:
        raise ValueError('profile must be research/compat/legacy')
    return (LEGACY_TOOLS if profile == 'legacy' else RESEARCH_TOOLS if profile == 'research'
            else LEGACY_TOOLS + RESEARCH_TOOLS)


def profile_from_args(args):
    if '--profile' in args:
        return args[args.index('--profile') + 1]
    return 'research' if '--catalog' in args else 'legacy'


def native_type(value, annotation):
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return any(native_type(value, part) for part in get_args(annotation))
    if origin is list:
        return type(value) is list and all(native_type(v, get_args(annotation)[0]) for v in value)
    if origin is dict:
        key_type, value_type = get_args(annotation)
        return type(value) is dict and all(native_type(k, key_type) and native_type(v, value_type)
                                          for k, v in value.items())
    return type(value) is annotation


def valid_arguments(function, arguments):
    """Validate before SDK coercion; bool must never pass an integer field."""
    if type(arguments) is not dict:
        return False
    parameters = inspect.signature(function).parameters
    hints = get_type_hints(function)
    if set(arguments) - set(parameters):
        return False
    if any(p.default is inspect.Parameter.empty and name not in arguments
           for name, p in parameters.items()):
        return False
    return all(native_type(value, hints[name]) for name, value in arguments.items())
