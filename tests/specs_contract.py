"""OpenAPI 3 / Swagger 2 spec pairs for tests/test_contract.py: one of every operation-level change."""

PET = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "tag": {"type": "string"}}}

OAS_OLD = {
    "openapi": "3.0.3",
    "info": {"title": "pets", "version": "1"},
    "paths": {
        "/pets": {
            "parameters": [{"$ref": "#/components/parameters/Trace"}],
            "get": {
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 100}},
                    {"name": "X-Old", "in": "header", "schema": {"type": "string"}},
                    {"name": "sort", "in": "query", "schema": {"type": "string"}},
                    {"name": "page", "in": "query", "required": True, "schema": {"type": "integer"}},
                    {"name": "q", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {"$ref": "#/components/responses/PetList"},
                    "404": {"description": "not found"},
                    "500": {"description": "error", "content": {"application/json": {"schema": {"type": "object"}}}},
                },
            },
            "post": {
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}},
                    "application/xml": {"schema": {"type": "object"}}}},
                "responses": {"201": {"description": "created", "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}},
                    "text/plain": {"schema": {"type": "string"}}}}},
            },
            "patch": {
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"200": {"description": "ok"}, "202": {"description": "accepted"}},
            },
            "delete": {"responses": {"204": {"description": "gone"}}},
        },
        "/owners": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
    "components": {
        "parameters": {"Trace": {"name": "X-Trace", "in": "header", "schema": {"type": "string"}}},
        "responses": {"PetList": {"description": "ok", "content": {"application/json": {
            "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}}}}},
        "schemas": {"Pet": PET, "Legacy": {"type": "string"}},
    },
}

OAS_NEW = {
    "openapi": "3.0.3",
    "info": {"title": "pets", "version": "2"},
    "paths": {
        "/pets": {
            "parameters": [{"$ref": "#/components/parameters/Trace"}],
            "get": {
                "parameters": [
                    {"name": "X-Trace", "in": "header", "required": True, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 50}},
                    {"name": "page", "in": "query", "schema": {"type": "integer"}},
                    {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "fields", "in": "query", "schema": {"type": "string"}},
                    {"name": "tenant", "in": "header", "required": True, "schema": {"type": "string"}},
                    {"name": "region", "in": "path", "schema": {"type": "string"}},
                    {"in": "query", "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {"$ref": "#/components/responses/PetList"},
                    "429": {"description": "slow down"},
                    "500": {"description": "error"},
                },
            },
            "post": {
                "requestBody": {"$ref": "#/components/requestBodies/NewPet"},
                "responses": {"201": {"description": "created", "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}},
            },
            "patch": {
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"200": {"description": "ok"}},
            },
            "put": {"responses": {"200": {"description": "ok"}}},
        },
        "/vets": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
    "components": {
        "parameters": {"Trace": {"name": "X-Trace", "in": "header", "schema": {"type": "string"}}},
        "requestBodies": {"NewPet": {"content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}},
            "text/plain": {"schema": {"type": "string"}}}}},
        "responses": {"PetList": {"description": "ok", "content": {"application/json": {
            "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}}}}},
        "schemas": {"Pet": PET, "Owner": {"type": "object"}},
    },
}

SW_OLD = {
    "swagger": "2.0",
    "info": {"title": "inv", "version": "1"},
    "paths": {
        "/items": {
            "get": {
                "parameters": [
                    {"name": "n", "in": "query", "type": "integer", "format": "int32", "minimum": 1, "maximum": 10},
                    {"name": "s", "in": "query", "type": "string", "minLength": 1, "maxLength": 5, "pattern": "^a",
                     "enum": ["a", "ab"]},
                    {"name": "ids", "in": "query", "type": "array", "items": {"type": "string"}},
                    {"name": "verbose", "in": "query", "type": "boolean", "description": "ignored by the diff"},
                ],
                "responses": {"200": {"description": "ok", "schema": {"$ref": "#/definitions/Item"}},
                              "default": {"description": "error"}},
            },
            "post": {
                "parameters": [{"name": "body", "in": "body", "required": True, "schema": {"$ref": "#/definitions/Item"}}],
                "responses": {"201": {"description": "created"}},
            },
        },
    },
    "definitions": {"Item": {"type": "object", "required": ["id"],
                             "properties": {"id": {"type": "string"}, "qty": {"type": "integer"}}},
                    "Gone": {"type": "object"}},
}

SW_NEW = {
    "swagger": "2.0",
    "info": {"title": "inv", "version": "2"},
    "paths": {
        "/items": {
            "get": {
                "parameters": [
                    {"name": "n", "in": "query", "type": "integer", "format": "int64", "minimum": 1, "maximum": 5},
                    {"name": "s", "in": "query", "type": "string", "minLength": 2, "maxLength": 10, "pattern": "^b",
                     "enum": ["a", "ab", "abc"]},
                    {"name": "ids", "in": "query", "type": "array", "items": {"type": "integer"}},
                    {"name": "verbose", "in": "query", "type": "boolean", "description": "still ignored"},
                ],
                "responses": {"200": {"description": "ok", "schema": {"$ref": "#/definitions/Item"}}},
            },
            "post": {
                "parameters": [{"name": "payload", "in": "body", "schema": {"$ref": "#/definitions/Item"}}],
                "responses": {"201": {"description": "created"}},
            },
        },
    },
    "definitions": {"Item": {"type": "object", "required": ["id", "qty"],
                             "properties": {"id": {"type": "string"}, "qty": {"type": "integer"}}},
                    "New": {"type": "object"}},
}
