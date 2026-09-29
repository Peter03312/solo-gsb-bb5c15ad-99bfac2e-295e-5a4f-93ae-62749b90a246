/* 内置示例：old/new 成对提供。既有 JSON 也有 YAML，演示两种格式输入。 */
window.SAMPLES = {
  breaking: {
    old: `{
  "openapi": "3.0.3",
  "info": { "title": "Petstore", "version": "1.4.0" },
  "paths": {
    "/pets": {
      "get": {
        "operationId": "listPets",
        "parameters": [
          { "name": "limit", "in": "query", "required": false,
            "schema": { "type": "integer", "maximum": 100 } }
        ],
        "responses": {
          "200": {
            "description": "宠物列表",
            "content": {
              "application/json": {
                "schema": {
                  "type": "array",
                  "items": { "$ref": "#/components/schemas/Pet" }
                }
              }
            }
          }
        }
      },
      "post": {
        "operationId": "createPet",
        "requestBody": {
          "required": false,
          "content": {
            "application/json": { "schema": { "$ref": "#/components/schemas/PetCreate" } }
          }
        },
        "responses": {
          "201": { "description": "创建成功" },
          "200": { "description": "旧的成功响应" }
        }
      }
    },
    "/pets/{petId}": {
      "get": {
        "operationId": "getPetById",
        "parameters": [
          { "name": "petId", "in": "path", "required": true,
            "schema": { "type": "string" } }
        ],
        "responses": {
          "200": {
            "description": "宠物详情",
            "content": {
              "application/json": { "schema": { "$ref": "#/components/schemas/Pet" } }
            }
          }
        }
      }
    }
  },
  "components": {
    "schemas": {
      "Pet": {
        "type": "object",
        "required": ["id", "name", "status"],
        "properties": {
          "id": { "type": "integer", "format": "int64" },
          "name": { "type": "string", "maxLength": 50 },
          "status": { "type": "string", "enum": ["available", "pending", "sold"] },
          "tags": {
            "type": "array",
            "items": { "$ref": "#/components/schemas/Tag" }
          }
        }
      },
      "Tag": {
        "type": "object",
        "properties": { "name": { "type": "string" } }
      },
      "PetCreate": {
        "type": "object",
        "required": ["name"],
        "properties": {
          "name": { "type": "string", "maxLength": 50 },
          "status": { "type": "string", "enum": ["available", "pending", "sold"] }
        }
      }
    }
  }
}`,
    new: `{
  "openapi": "3.0.3",
  "info": { "title": "Petstore", "version": "2.0.0" },
  "paths": {
    "/pets": {
      "get": {
        "operationId": "searchPets",
        "parameters": [
          { "name": "limit", "in": "query", "required": true,
            "schema": { "type": "integer", "maximum": 50 } },
          { "name": "tenant", "in": "header", "required": true,
            "schema": { "type": "string" } }
        ],
        "responses": {
          "200": {
            "description": "宠物列表",
            "content": {
              "application/json": {
                "schema": {
                  "type": "array",
                  "items": { "$ref": "#/components/schemas/Pet" }
                }
              }
            }
          }
        }
      },
      "post": {
        "operationId": "createPet",
        "requestBody": {
          "required": true,
          "content": {
            "application/json": { "schema": { "$ref": "#/components/schemas/PetCreate" } }
          }
        },
        "responses": {
          "201": { "description": "创建成功" }
        }
      }
    },
    "/pets/{petId}": {
      "get": {
        "operationId": "getPetById",
        "parameters": [
          { "name": "petId", "in": "path", "required": true,
            "schema": { "type": "integer" } }
        ],
        "responses": {
          "200": {
            "description": "宠物详情",
            "content": {
              "application/json": { "schema": { "$ref": "#/components/schemas/Pet" } }
            }
          }
        }
      }
    },
    "/admin/pets/{petId}/force-delete": {
      "post": {
        "operationId": "forceDeletePet",
        "responses": { "204": { "description": "已删除" } }
      }
    }
  },
  "components": {
    "schemas": {
      "Pet": {
        "type": "object",
        "required": ["id", "name", "status", "owner"],
        "properties": {
          "id": { "type": "string", "format": "uuid" },
          "name": { "type": "string", "maxLength": 30 },
          "status": { "type": "string", "enum": ["available", "sold"] },
          "owner": { "$ref": "#/components/schemas/User" },
          "tags": {
            "type": "array",
            "items": { "$ref": "#/components/schemas/Tag" }
          }
        }
      },
      "Tag": {
        "type": "object",
        "properties": { "name": { "type": "string" } }
      },
      "User": {
        "type": "object",
        "required": ["id"],
        "properties": { "id": { "type": "integer" } }
      },
      "PetCreate": {
        "type": "object",
        "required": ["name", "status"],
        "properties": {
          "name": { "type": "string", "maxLength": 30 },
          "status": { "type": "string", "enum": ["available", "sold"] }
        }
      }
    }
  }
}`
  },

  cyclic: {
    old: `openapi: 3.0.3
info:
  title: Forest API（循环引用示例）
  version: 1.0.0
paths:
  /nodes/{id}:
    get:
      operationId: getNode
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: string }
      responses:
        '200':
          description: 树节点
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Node'
components:
  schemas:
    Node:
      type: object
      required: [id, name]
      properties:
        id: { type: string }
        name: { type: string, maxLength: 200 }
        parent:
          $ref: '#/components/schemas/Node'
        children:
          type: array
          items:
            $ref: '#/components/schemas/Node'
        meta:
          $ref: '#/components/schemas/NodeMeta'
    NodeMeta:
      type: object
      properties:
        createdBy: { type: string }
        links:
          type: array
          items:
            $ref: '#/components/schemas/NodeLink'
    NodeLink:
      type: object
      properties:
        node:
          $ref: '#/components/schemas/Node'
        kind: { type: string, enum: [child, alias] }
`,
    new: `openapi: 3.0.3
info:
  title: Forest API（循环引用示例）
  version: 2.0.0
paths:
  /nodes/{id}:
    get:
      operationId: getNode
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: string }
      responses:
        '200':
          description: 树节点
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Node'
components:
  schemas:
    Node:
      type: object
      required: [id, name, depth]
      properties:
        id: { type: integer }
        name: { type: string, maxLength: 100 }
        depth: { type: integer, minimum: 0 }
        parent:
          $ref: '#/components/schemas/Node'
        children:
          type: array
          items:
            $ref: '#/components/schemas/Node'
        meta:
          $ref: '#/components/schemas/NodeMeta'
    NodeMeta:
      type: object
      properties:
        createdBy: { type: integer }
        links:
          type: array
          items:
            $ref: '#/components/schemas/NodeLink'
    NodeLink:
      type: object
      properties:
        node:
          $ref: '#/components/schemas/Node'
        kind: { type: string, enum: [child] }
`
  },

  compatible: {
    old: `{
  "openapi": "3.0.3",
  "info": { "title": "Orders", "version": "1.0.0" },
  "servers": [{ "url": "https://api.example.com/v1" }],
  "paths": {
    "/orders": {
      "get": {
        "operationId": "listOrders",
        "parameters": [
          { "name": "page", "in": "query", "required": false,
            "schema": { "type": "integer", "minimum": 1 } }
        ],
        "responses": {
          "200": {
            "description": "订单列表",
            "content": {
              "application/json": {
                "schema": {
                  "type": "object",
                  "required": ["items"],
                  "properties": {
                    "items": {
                      "type": "array",
                      "items": { "$ref": "#/components/schemas/Order" }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  },
  "components": {
    "schemas": {
      "Order": {
        "type": "object",
        "required": ["id", "amount"],
        "properties": {
          "id": { "type": "string" },
          "amount": { "type": "number", "minimum": 0 },
          "remark": { "type": "string", "maxLength": 200 }
        }
      }
    }
  }
}`,
    new: `{
  "openapi": "3.0.3",
  "info": { "title": "Orders", "version": "1.1.0" },
  "servers": [
    { "url": "https://api.example.com/v1" },
    { "url": "https://api-cn.example.com/v1" }
  ],
  "paths": {
    "/orders": {
      "get": {
        "operationId": "listOrders",
        "parameters": [
          { "name": "page", "in": "query", "required": false,
            "schema": { "type": "integer", "minimum": 1 } },
          { "name": "size", "in": "query", "required": false,
            "schema": { "type": "integer", "minimum": 1, "maximum": 100 } }
        ],
        "responses": {
          "200": {
            "description": "订单列表",
            "content": {
              "application/json": {
                "schema": {
                  "type": "object",
                  "required": ["items"],
                  "properties": {
                    "items": {
                      "type": "array",
                      "items": { "$ref": "#/components/schemas/Order" }
                    },
                    "nextCursor": { "type": "string" }
                  }
                }
              }
            }
          },
          "429": { "description": "触发限流" }
        }
      }
    },
    "/orders/{orderId}": {
      "get": {
        "operationId": "getOrder",
        "parameters": [
          { "name": "orderId", "in": "path", "required": true,
            "schema": { "type": "string" } }
        ],
        "responses": {
          "200": {
            "description": "订单详情",
            "content": {
              "application/json": {
                "schema": { "$ref": "#/components/schemas/Order" }
              }
            }
          }
        }
      }
    }
  },
  "components": {
    "schemas": {
      "Order": {
        "type": "object",
        "required": ["id", "amount"],
        "properties": {
          "id": { "type": "string" },
          "amount": { "type": "number", "minimum": 0 },
          "remark": { "type": "string", "maxLength": 200 },
          "createdAt": { "type": "string", "format": "date-time" }
        }
      }
    }
  }
}`
  },

  auth: {
    old: `openapi: 3.0.3
info: { title: "Authz API", version: "1.0.0" }
security:
  - oauth2: ["read"]
paths:
  /ping:
    get:
      operationId: ping
      security: []
      responses:
        '200': { description: 健康检查 }
  /public:
    get:
      operationId: listPublic
      responses:
        '200': { description: 公开列表 }
  /pets:
    get:
      operationId: listPets
      security:
        - oauth2: ["pets:read"]
      responses:
        '200': { description: 宠物列表 }
    post:
      operationId: createPet
      security:
        - oauth2: ["pets:write"]
      responses:
        '201': { description: 已创建 }
  /pets/{id}:
    delete:
      operationId: deletePet
      security:
        - apiKey: []
      responses:
        '204': { description: 已删除 }
components:
  securitySchemes:
    oauth2:
      type: oauth2
      flows:
        authorizationCode:
          authorizationUrl: https://auth.example.com/authorize
          tokenUrl: https://auth.example.com/token
          scopes:
            read: 只读
            pets:read: 读取宠物
            pets:write: 写宠物
    apiKey:
      type: apiKey
      in: header
      name: X-API-Key
`,
    new: `openapi: 3.0.3
info: { title: "Authz API", version: "2.0.0" }
security:
  - oauth2: ["read"]
paths:
  /ping:
    get:
      operationId: ping
      security: [{}]
      responses:
        '200': { description: 健康检查 }
  /public:
    get:
      operationId: listPublic
      security:
        - oauth2: ["pets:read"]
      responses:
        '200': { description: 公开列表 }
  /pets:
    get:
      operationId: listPets
      security:
        - oauth2: ["pets:read", "pets:admin"]
      responses:
        '200': { description: 宠物列表 }
    post:
      operationId: createPet
      security:
        - apiKey: []
      responses:
        '201': { description: 已创建 }
  /pets/{id}:
    delete:
      operationId: deletePet
      security:
        - apiKey: []
        - oauth2: ["admin"]
      responses:
        '204': { description: 已删除 }
components:
  securitySchemes:
    oauth2:
      type: oauth2
      flows:
        authorizationCode:
          authorizationUrl: https://auth.example.com/authorize
          tokenUrl: https://auth.example.com/token
          scopes:
            read: 只读
            pets:read: 读取宠物
            pets:write: 写宠物
            pets:admin: 管理宠物
            admin: 管理员
    apiKey:
      type: apiKey
      in: header
      name: X-API-Key
`
  },

  enum: {
    old: `openapi: 3.0.3
info: { title: "Tickets", version: "3.0.0" }
paths:
  /tickets:
    post:
      operationId: createTicket
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/TicketInput'
      responses:
        '201':
          description: 已创建
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Ticket'
        '400':
          description: 参数错误
components:
  schemas:
    TicketInput:
      type: object
      required: [title, priority]
      properties:
        title: { type: string, minLength: 1, maxLength: 100 }
        priority: { type: string, enum: [low, normal, high, urgent] }
        age: { type: integer, minimum: 0, maximum: 120 }
    Ticket:
      type: object
      required: [id, priority, state]
      properties:
        id: { type: string }
        priority: { type: string, enum: [low, normal, high, urgent] }
        state: { type: string, enum: [open, closed] }
`,
    new: `openapi: 3.0.3
info: { title: "Tickets", version: "4.0.0" }
paths:
  /tickets:
    post:
      operationId: createTicket
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/TicketInput'
      responses:
        '201':
          description: 已创建
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Ticket'
components:
  schemas:
    TicketInput:
      type: object
      required: [title, priority]
      properties:
        title: { type: string, minLength: 4, maxLength: 80 }
        priority: { type: string, enum: [normal, high] }
        age: { type: integer, minimum: 18, maximum: 100 }
    Ticket:
      type: object
      required: [id, priority, state]
      properties:
        id: { type: string }
        priority: { type: string, enum: [low, normal, high, urgent, critical] }
        state: { type: string, enum: [open, closed, archived] }
`
  }
};
