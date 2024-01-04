// Package docs GENERATED BY SWAG; DO NOT EDIT
// This file was generated by swaggo/swag
package docs

import "github.com/swaggo/swag"

const docTemplate = `{
    "schemes": {{ marshal .Schemes }},
    "swagger": "2.0",
    "info": {
        "description": "{{escape .Description}}",
        "title": "{{.Title}}",
        "contact": {},
        "version": "{{.Version}}"
    },
    "host": "{{.Host}}",
    "basePath": "{{.BasePath}}",
    "paths": {
        "/api/artifact/sync-image": {
            "post": {
                "description": "sync",
                "consumes": [
                    "application/json"
                ],
                "produces": [
                    "application/json"
                ],
                "tags": [
                    "artifact"
                ],
                "summary": "sync hotfix image to dockerhub",
                "parameters": [
                    {
                        "description": "image sync to public, only hotfix is accepted right now",
                        "name": "ImageSyncRequest",
                        "in": "body",
                        "required": true,
                        "schema": {
                            "$ref": "#/definitions/service.ImageSyncRequest"
                        }
                    }
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "schema": {
                            "$ref": "#/definitions/service.ImageSyncRequest"
                        }
                    },
                    "400": {
                        "description": "Bad Request",
                        "schema": {
                            "$ref": "#/definitions/controller.HTTPError"
                        }
                    },
                    "500": {
                        "description": "Internal Server Error",
                        "schema": {
                            "$ref": "#/definitions/controller.HTTPError"
                        }
                    }
                }
            }
        },
        "/api/devbuilds": {
            "get": {
                "description": "list devbuild",
                "produces": [
                    "application/json"
                ],
                "tags": [
                    "devbuild"
                ],
                "summary": "list devbuild",
                "parameters": [
                    {
                        "type": "integer",
                        "default": 10,
                        "description": "the size limit of items",
                        "name": "size",
                        "in": "query"
                    },
                    {
                        "type": "integer",
                        "default": 0,
                        "description": "the start position of items",
                        "name": "offset",
                        "in": "query"
                    },
                    {
                        "type": "boolean",
                        "description": "filter hotfix",
                        "name": "hotfix",
                        "in": "query"
                    }
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "schema": {
                            "type": "array",
                            "items": {
                                "$ref": "#/definitions/service.DevBuild"
                            }
                        }
                    },
                    "400": {
                        "description": "Bad Request",
                        "schema": {
                            "$ref": "#/definitions/controller.HTTPError"
                        }
                    }
                }
            },
            "post": {
                "description": "create and trigger devbuild",
                "consumes": [
                    "application/json"
                ],
                "produces": [
                    "application/json"
                ],
                "tags": [
                    "devbuild"
                ],
                "summary": "create and trigger devbuild",
                "parameters": [
                    {
                        "description": "build to create, only spec filed is required, others are ignored",
                        "name": "DevBuild",
                        "in": "body",
                        "required": true,
                        "schema": {
                            "$ref": "#/definitions/service.DevBuild"
                        }
                    },
                    {
                        "type": "boolean",
                        "default": false,
                        "description": "dry run",
                        "name": "dryrun",
                        "in": "query"
                    }
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "schema": {
                            "$ref": "#/definitions/service.DevBuild"
                        }
                    },
                    "400": {
                        "description": "Bad Request",
                        "schema": {
                            "$ref": "#/definitions/controller.HTTPError"
                        }
                    },
                    "500": {
                        "description": "Internal Server Error",
                        "schema": {
                            "$ref": "#/definitions/controller.HTTPError"
                        }
                    }
                }
            }
        },
        "/api/devbuilds/{id}": {
            "get": {
                "description": "get devbuild",
                "produces": [
                    "application/json"
                ],
                "tags": [
                    "devbuild"
                ],
                "summary": "get devbuild",
                "parameters": [
                    {
                        "type": "integer",
                        "description": "id of build",
                        "name": "id",
                        "in": "path",
                        "required": true
                    },
                    {
                        "type": "boolean",
                        "default": false,
                        "description": "whether sync with jenkins",
                        "name": "sync",
                        "in": "query"
                    }
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "schema": {
                            "$ref": "#/definitions/service.DevBuild"
                        }
                    },
                    "400": {
                        "description": "Bad Request",
                        "schema": {
                            "$ref": "#/definitions/controller.HTTPError"
                        }
                    },
                    "500": {
                        "description": "Internal Server Error",
                        "schema": {
                            "$ref": "#/definitions/controller.HTTPError"
                        }
                    }
                }
            },
            "put": {
                "description": "update the status field of a build",
                "consumes": [
                    "application/json"
                ],
                "produces": [
                    "application/json"
                ],
                "tags": [
                    "devbuild"
                ],
                "summary": "update devbuild status",
                "parameters": [
                    {
                        "type": "integer",
                        "description": "id of build",
                        "name": "id",
                        "in": "path",
                        "required": true
                    },
                    {
                        "description": "build to update",
                        "name": "DevBuild",
                        "in": "body",
                        "required": true,
                        "schema": {
                            "$ref": "#/definitions/service.DevBuild"
                        }
                    },
                    {
                        "type": "boolean",
                        "default": false,
                        "description": "dry run",
                        "name": "dryrun",
                        "in": "query"
                    }
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "schema": {
                            "$ref": "#/definitions/service.DevBuild"
                        }
                    },
                    "400": {
                        "description": "Bad Request",
                        "schema": {
                            "$ref": "#/definitions/controller.HTTPError"
                        }
                    },
                    "500": {
                        "description": "Internal Server Error",
                        "schema": {
                            "$ref": "#/definitions/controller.HTTPError"
                        }
                    }
                }
            }
        },
        "/api/devbuilds/{id}/rerun": {
            "post": {
                "description": "rerun devbuild",
                "produces": [
                    "application/json"
                ],
                "tags": [
                    "devbuild"
                ],
                "summary": "rerun devbuild",
                "parameters": [
                    {
                        "type": "integer",
                        "description": "id of build",
                        "name": "id",
                        "in": "path",
                        "required": true
                    },
                    {
                        "type": "boolean",
                        "default": false,
                        "description": "dry run",
                        "name": "dryrun",
                        "in": "query"
                    }
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "schema": {
                            "$ref": "#/definitions/service.DevBuild"
                        }
                    },
                    "400": {
                        "description": "Bad Request",
                        "schema": {
                            "$ref": "#/definitions/controller.HTTPError"
                        }
                    },
                    "500": {
                        "description": "Internal Server Error",
                        "schema": {
                            "$ref": "#/definitions/controller.HTTPError"
                        }
                    }
                }
            }
        }
    },
    "definitions": {
        "controller.HTTPError": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "integer"
                },
                "message": {
                    "type": "string"
                }
            }
        },
        "service.BinArtifact": {
            "type": "object",
            "properties": {
                "component": {
                    "type": "string"
                },
                "oras": {
                    "$ref": "#/definitions/service.OrasFile"
                },
                "platform": {
                    "type": "string"
                },
                "sha256URL": {
                    "type": "string"
                },
                "url": {
                    "type": "string"
                }
            }
        },
        "service.BranchCreateReq": {
            "type": "object",
            "properties": {
                "baseVersion": {
                    "type": "string"
                },
                "prod": {
                    "$ref": "#/definitions/service.Product"
                }
            }
        },
        "service.BranchCreateResp": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string"
                },
                "branchURL": {
                    "type": "string"
                }
            }
        },
        "service.BuildReport": {
            "type": "object",
            "properties": {
                "binaries": {
                    "type": "array",
                    "items": {
                        "$ref": "#/definitions/service.BinArtifact"
                    }
                },
                "gitHash": {
                    "type": "string"
                },
                "images": {
                    "type": "array",
                    "items": {
                        "$ref": "#/definitions/service.ImageArtifact"
                    }
                },
                "pluginGitHash": {
                    "type": "string"
                },
                "printedVersion": {
                    "type": "string"
                }
            }
        },
        "service.BuildStatus": {
            "type": "string",
            "enum": [
                "PENDING",
                "PROCESSING",
                "ABORTED",
                "SUCCESS",
                "FAILURE",
                "ERROR"
            ],
            "x-enum-varnames": [
                "BuildStatusPending",
                "BuildStatusProcessing",
                "BuildStatusAborted",
                "BuildStatusSuccess",
                "BuildStatusFailure",
                "BuildStatusError"
            ]
        },
        "service.DevBuild": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer"
                },
                "meta": {
                    "$ref": "#/definitions/service.DevBuildMeta"
                },
                "spec": {
                    "$ref": "#/definitions/service.DevBuildSpec"
                },
                "status": {
                    "$ref": "#/definitions/service.DevBuildStatus"
                }
            }
        },
        "service.DevBuildMeta": {
            "type": "object",
            "properties": {
                "createdAt": {
                    "type": "string"
                },
                "createdBy": {
                    "type": "string"
                },
                "updatedAt": {
                    "type": "string"
                }
            }
        },
        "service.DevBuildSpec": {
            "type": "object",
            "properties": {
                "buildEnv": {
                    "type": "string"
                },
                "builderImg": {
                    "type": "string"
                },
                "edition": {
                    "$ref": "#/definitions/service.ProductEdition"
                },
                "features": {
                    "type": "string"
                },
                "gitRef": {
                    "type": "string"
                },
                "githubRepo": {
                    "type": "string"
                },
                "isHotfix": {
                    "type": "boolean"
                },
                "isPushGCR": {
                    "type": "boolean"
                },
                "pluginGitRef": {
                    "type": "string"
                },
                "preferedEngine": {
                    "$ref": "#/definitions/service.PipelineEngine"
                },
                "product": {
                    "$ref": "#/definitions/service.Product"
                },
                "productBaseImg": {
                    "type": "string"
                },
                "productDockerfile": {
                    "type": "string"
                },
                "targetImg": {
                    "type": "string"
                },
                "version": {
                    "type": "string"
                }
            }
        },
        "service.DevBuildStatus": {
            "type": "object",
            "properties": {
                "buildReport": {
                    "$ref": "#/definitions/service.BuildReport"
                },
                "errMsg": {
                    "type": "string"
                },
                "pipelineBuildID": {
                    "type": "integer"
                },
                "pipelineEndAt": {
                    "type": "string"
                },
                "pipelineStartAt": {
                    "type": "string"
                },
                "pipelineViewURL": {
                    "type": "string"
                },
                "status": {
                    "$ref": "#/definitions/service.BuildStatus"
                },
                "tektonStatus": {
                    "$ref": "#/definitions/service.TektonStatus"
                }
            }
        },
        "service.ImageArtifact": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string"
                },
                "url": {
                    "type": "string"
                }
            }
        },
        "service.ImageSyncRequest": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string"
                },
                "target": {
                    "type": "string"
                }
            }
        },
        "service.OrasArtifact": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                },
                "repo": {
                    "type": "string"
                },
                "tag": {
                    "type": "string"
                }
            }
        },
        "service.OrasFile": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string"
                },
                "repo": {
                    "type": "string"
                },
                "tag": {
                    "type": "string"
                }
            }
        },
        "service.PipelineEngine": {
            "type": "string",
            "enum": [
                "jenkins",
                "tekton"
            ],
            "x-enum-varnames": [
                "JenkinsEngine",
                "TektonEngine"
            ]
        },
        "service.Product": {
            "type": "string",
            "enum": [
                "tidb",
                "enterprise-plugin",
                "tikv",
                "pd",
                "tiflash",
                "br",
                "dumpling",
                "tidb-lightning",
                "ticdc",
                "dm",
                "tidb-binlog",
                "tidb-tools",
                "ng-monitoring",
                "tidb-dashboard",
                "drainer",
                "pump",
                ""
            ],
            "x-enum-varnames": [
                "ProductTidb",
                "ProductEnterprisePlugin",
                "ProductTikv",
                "ProductPd",
                "ProductTiflash",
                "ProductBr",
                "ProductDumpling",
                "ProductTidbLightning",
                "ProductTicdc",
                "ProductDm",
                "ProductTidbBinlog",
                "ProductTidbTools",
                "ProductNgMonitoring",
                "ProductTidbDashboard",
                "ProductDrainer",
                "ProductPump",
                "ProductUnknown"
            ]
        },
        "service.ProductEdition": {
            "type": "string",
            "enum": [
                "enterprise",
                "community"
            ],
            "x-enum-varnames": [
                "EnterpriseEdition",
                "CommunityEdition"
            ]
        },
        "service.TagCreateReq": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string"
                },
                "prod": {
                    "$ref": "#/definitions/service.Product"
                }
            }
        },
        "service.TagCreateResp": {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string"
                },
                "tagURL": {
                    "type": "string"
                }
            }
        },
        "service.TektonPipeline": {
            "type": "object",
            "properties": {
                "gitHash": {
                    "type": "string"
                },
                "images": {
                    "type": "array",
                    "items": {
                        "$ref": "#/definitions/service.ImageArtifact"
                    }
                },
                "name": {
                    "type": "string"
                },
                "orasArtifacts": {
                    "type": "array",
                    "items": {
                        "$ref": "#/definitions/service.OrasArtifact"
                    }
                },
                "pipelineEndAt": {
                    "type": "string"
                },
                "pipelineStartAt": {
                    "type": "string"
                },
                "platform": {
                    "type": "string"
                },
                "status": {
                    "$ref": "#/definitions/service.BuildStatus"
                },
                "url": {
                    "type": "string"
                }
            }
        },
        "service.TektonStatus": {
            "type": "object",
            "properties": {
                "buildReport": {
                    "$ref": "#/definitions/service.BuildReport"
                },
                "pipelineEndAt": {
                    "type": "string"
                },
                "pipelineStartAt": {
                    "type": "string"
                },
                "pipelines": {
                    "type": "array",
                    "items": {
                        "$ref": "#/definitions/service.TektonPipeline"
                    }
                },
                "status": {
                    "$ref": "#/definitions/service.BuildStatus"
                }
            }
        }
    }
}`

// SwaggerInfo holds exported Swagger Info so clients can modify it
var SwaggerInfo = &swag.Spec{
	Version:          "",
	Host:             "",
	BasePath:         "",
	Schemes:          []string{},
	Title:            "",
	Description:      "",
	InfoInstanceName: "swagger",
	SwaggerTemplate:  docTemplate,
}

func init() {
	swag.Register(SwaggerInfo.InstanceName(), SwaggerInfo)
}
