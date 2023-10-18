"""
    Swagger V3 Generator
    ~~~~~~~~~~~~~~~~~~~~

    Class for converting a handler registry into a Swagger v3 specification.

    :copyright: Copyright 2019 PlanGrid, Inc., see AUTHORS.
    :license: MIT, see LICENSE for details.
"""
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence

from marshmallow import Schema

from flask_rebar.utils.defaults import USE_DEFAULT
from flask_rebar.swagger_generation import swagger_words as sw
from flask_rebar.swagger_generation.authenticator_to_swagger import (
    AuthenticatorConverterRegistry,
)
from flask_rebar.swagger_generation.swagger_generator_base import SwaggerGenerator
from flask_rebar.swagger_generation.swagger_objects import Server
from flask_rebar.swagger_generation.generator_utils import (
    format_path_for_swagger,
    verify_parameters_are_the_same,
    get_response_description,
    get_unique_schema_definitions,
    recursively_convert_dict_to_ordered_dict,
    get_ref_schema,
    get_unique_authenticators,
)
from flask_rebar.swagger_generation.marshmallow_to_swagger import (
    get_swagger_title,
    get_schema_fields,
    ConverterRegistry,
)
from flask_rebar.validation import Error

if TYPE_CHECKING:
    from flask_rebar import Tag
    from flask_rebar.rebar import HandlerRegistry, PathDefinition


class SwaggerV3Generator(SwaggerGenerator):
    """Generates a v3.1.0 Swagger specification from a Rebar object.

    Not all things are retrievable from the Rebar object, so this
    guy also needs some additional information to complete the job.

    :param ConverterRegistry query_string_converter_registry:
    :param ConverterRegistry request_body_converter_registry:
    :param ConverterRegistry headers_converter_registry:
    :param ConverterRegistry response_converter_registry:
        ConverterRegistrys that will be used to convert Marshmallow schemas
        to the corresponding types of swagger objects. These default to the
        global registries.

    :param Sequence[Tag] tags:
        A list of tags used by the specification with additional metadata.
    :param Sequence[Server] servers:
        A list of Server Objects to set as the server metadata for the specification.
    """

    _open_api_version = "3.1.0"

    def __init__(
        self,
        version: str = "1.0.0",
        title: str = "My API",
        description: str = "",
        query_string_converter_registry: Optional[ConverterRegistry] = None,
        request_body_converter_registry: Optional[ConverterRegistry] = None,
        headers_converter_registry: Optional[ConverterRegistry] = None,
        response_converter_registry: Optional[ConverterRegistry] = None,
        tags: Optional[Sequence["Tag"]] = None,
        servers: Optional[Sequence[Server]] = None,
        default_response_schema: Schema = Error(),
        authenticator_converter_registry: Optional[
            AuthenticatorConverterRegistry
        ] = None,
        include_hidden: bool = False,
    ):
        super().__init__(
            openapi_major_version=3,
            version=version,
            title=title,
            description=description,
            default_response_schema=default_response_schema,
            query_string_converter_registry=query_string_converter_registry,
            request_body_converter_registry=request_body_converter_registry,
            headers_converter_registry=headers_converter_registry,
            response_converter_registry=response_converter_registry,
            authenticator_converter_registry=authenticator_converter_registry,
            include_hidden=include_hidden,
        )
        self.tags = tags
        self.servers = servers
        self._ref_base = "#/components/schemas"

    def generate_swagger(
        self, registry: "HandlerRegistry", host: Optional[str] = None
    ) -> Dict[str, Any]:
        return self.generate(registry=registry, host=host)

    def generate(
        self,
        registry: "HandlerRegistry",
        host: Optional[str] = None,
        sort_keys: bool = True,
    ) -> Dict[str, Any]:
        """Generates a Swagger specification from a Rebar instance.

        :param flask_rebar.rebar.HandlerRegistry registry:
        :param str host: Adds this host as a Server Object for the service
        :param bool sort_keys: Use OrderedDicts sorted by keys instead of dicts
        :rtype: dict
        """

        components = self._get_components(registry=registry)

        default_security = []
        for authenticator in registry.default_authenticators:
            default_security.extend(
                self.authenticator_converter.get_security_requirements(authenticator)
            )

        paths = self._get_paths(
            paths=registry.paths,
            default_headers_schema=registry.default_headers_schema,
            default_security=default_security,
        )

        swagger: Dict[str, Any] = {
            sw.openapi: self.get_open_api_version(),
            sw.info: self._get_info(),
            sw.paths: paths,
            sw.components: components,
        }
        if default_security:
            swagger[sw.security] = default_security

        if self.tags:
            swagger[sw.tags] = [tag.as_swagger() for tag in self.tags]

        servers = list(self.servers or [])

        if host:
            servers.append(Server(url=host))

        if servers:
            swagger[sw.servers] = [server.as_swagger() for server in servers]

        if sort_keys:
            # Sort the swagger we generated by keys to produce a consistent output.
            swagger = recursively_convert_dict_to_ordered_dict(swagger)

        return swagger

    def _get_paths(
        self,
        paths: Dict[str, Dict[str, "PathDefinition"]],
        default_headers_schema: Optional[Schema],
        default_security: Optional[Any] = None,
    ) -> Dict[str, Any]:
        path_definitions: Dict[str, Any] = {}

        for path, methods in paths.items():
            spec_path, path_args = format_path_for_swagger(path)

            if not self.include_hidden and all(
                d.hidden for method, d in methods.items()
            ):
                continue

            # Different Flask paths might correspond to the same Swagger path
            # because of Flask URL path converters. In this case, let's just
            # work off the same path definitions.
            if spec_path in path_definitions:
                path_definition = path_definitions[spec_path]
            else:
                path_definitions[spec_path] = path_definition = {}

            if path_args:
                path_params = [
                    {
                        sw.name: path_arg.name,
                        sw.required: True,
                        sw.in_: sw.path,
                        sw.style: sw.simple,
                        sw.schema: {
                            sw.type_: self.flask_converters_to_swagger_types[
                                path_arg.type
                            ]
                        },
                    }
                    for path_arg in path_args
                ]

                # We have to check for an ugly case here. If different Flask
                # paths that map to the same Swagger path use different URL
                # converters for the same parameter, we have a problem. Let's
                # just throw an error in this case.
                if sw.parameters in path_definition:
                    verify_parameters_are_the_same(
                        path_definition[sw.parameters], path_params
                    )

                path_definition[sw.parameters] = path_params

            for method, d in methods.items():
                if not self.include_hidden and d.hidden:
                    continue

                responses_definition = {
                    sw.default: self._get_response_definition(
                        self.default_response_schema
                    )
                }

                if d.response_body_schema:
                    for status_code, schema in d.response_body_schema.items():
                        if schema is not None:
                            response_definition = self._get_response_definition(schema)
                        else:
                            response_definition = {sw.description: "No response body."}

                        responses_definition[str(status_code)] = response_definition

                parameters_definition = []

                if d.query_string_schema:
                    parameters_definition.extend(
                        self._convert_schema_to_list_of_parameters(
                            schema=d.query_string_schema,
                            converter=self._query_string_converter,
                            in_=sw.query,
                        )
                    )

                if d.headers_schema is USE_DEFAULT:
                    headers_schema = default_headers_schema
                else:
                    headers_schema = d.headers_schema

                if headers_schema:
                    parameters_definition.extend(
                        self._convert_schema_to_list_of_parameters(
                            schema=headers_schema,
                            converter=self._headers_converter,
                            in_=sw.header,
                        )
                    )

                request_body = None

                if d.request_body_schema:
                    schema = d.request_body_schema

                    request_body = {
                        sw.required: True,
                        sw.content: {
                            "application/json": {
                                sw.schema: get_ref_schema(self._ref_base, schema)
                            }
                        },
                    }

                method_lower = method.lower()
                path_definition[method_lower] = {
                    sw.operation_id: d.endpoint or get_swagger_title(d.func),
                    sw.responses: responses_definition,
                }

                if d.func.__doc__:
                    path_definition[method_lower][sw.description] = d.func.__doc__

                if parameters_definition:
                    path_definition[method_lower][sw.parameters] = parameters_definition

                if request_body:
                    path_definition[method_lower][sw.request_body] = request_body

                if not d.authenticators:
                    path_definition[method_lower][sw.security] = []
                else:
                    non_default = False
                    security = []
                    for authenticator in d.authenticators:
                        if authenticator is not USE_DEFAULT:
                            security.extend(
                                self.authenticator_converter.get_security_requirements(
                                    authenticator
                                )
                            )
                            non_default = True
                        elif default_security is not None:
                            security.extend(default_security)
                    if non_default:
                        path_definition[method_lower][sw.security] = security

                if d.tags:
                    path_definition[method_lower][sw.tags] = d.tags

                if d.summary:
                    path_definition[method_lower][sw.summary] = d.summary

        return path_definitions

    def _get_response_definition(self, schema: Schema) -> Dict[str, Any]:
        return {
            sw.description: get_response_description(schema),
            sw.content: {
                "application/json": {sw.schema: get_ref_schema(self._ref_base, schema)}
            },
        }

    def _get_components(self, registry: "HandlerRegistry") -> Dict[str, Any]:
        """

        :param flask_rebar.rebar.HandlerRegistry registry:
        :return:
        """
        components = {}

        schemas = get_unique_schema_definitions(
            registry=registry,
            base=self._ref_base,
            default_response_schema=self.default_response_schema,
            response_converter=self._response_converter,
            request_body_converter=self._request_body_converter,
        )
        if schemas:
            components[sw.schemas] = schemas

        security_schemes = {}
        authenticators = get_unique_authenticators(registry)
        for authenticator in authenticators:
            # We should probably eventually check that scheme with the same name are identical
            # rather than just overwriting the existing scheme definition.
            security_schemes.update(
                self.authenticator_converter.get_security_schemes(authenticator)
            )
        if security_schemes:
            components[sw.security_schemes] = security_schemes

        return components

    def _convert_schema_to_list_of_parameters(
        self, schema: Schema, converter: Callable, in_: str
    ) -> List[Dict[str, Any]]:
        """Swagger is only _based_ on JSONSchema. Query string and header parameters
        are represented as list, not as an object. This converts a JSONSchema
        object (as return by the converters) to a list of parameters suitable for
        swagger.

        :param marshmallow.Schema schema:
        :param str in_: 'query' or 'header'
        :rtype: list[dict]
        """
        parameters = []

        for prop, field in get_schema_fields(schema):
            jsonschema = converter(field)

            # Pardon the ugliness.
            # We need the "explode" key to be at the parameters level, not at the schema level.
            explode = jsonschema.pop(sw.explode, None)
            description = jsonschema.pop(sw.description, None)
            style = jsonschema.pop(sw.style, None)

            parameter = {
                sw.name: prop,
                sw.in_: in_,
                sw.schema: jsonschema,
                sw.required: bool(field.required),
            }

            if explode is not None:
                parameter[sw.explode] = explode
            if description is not None:
                parameter[sw.description] = description
            if style is not None:
                parameter[sw.style] = style

            parameters.append(parameter)

        return parameters
