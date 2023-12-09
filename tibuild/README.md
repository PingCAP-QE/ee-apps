# TiBuild
[![Language](https://img.shields.io/badge/Language-Go-blue.svg)](https://golang.org/)

This repository is a build platform for PingCAP, Welcome bros!

## Design
[click and jump](https://pingcap.feishu.cn/wiki/wikcndnDJhhpnNZb5wivmJPPHJb)

## Technologies
+ Backend: Golang & [Gin](https://github.com/gin-gonic/gin)
+ Database: [Mysql of Gorm](https://github.com/go-gorm/gorm)
+ Common Utils: [Github](https://github.com/google/go-github) & [Config](https://github.com/jinzhu/configor)
+ Deployments: Docker & Kubernetes
+ Frontend: [Create-React-App for framework](https://github.com/facebook/create-react-app) & [Material-UI/MUI for components](https://github.com/mui-org/material-ui) & [Axios for remote procedure call](https://github.com/axios/axios)

## Quick Start
```
git clone https://github.com/PingCAP-QE/ee-apps.git
cd ee-apps/tibuild/
# create a config.yaml in root path (detail like /tibuild/config_example.yaml)
make run
```
After waiting a few seconds, application is available and can be visited in the browser:[localhost:8080](http://localhost:8080/)

## User Guide
[click and jump](https://pingcap.feishu.cn/wiki/wikcnlRSh8dyOoHGbMQEJN9x77d)

## File Structure
```
tibuild
├── .gitignore
├── README.md
├── go.mod             # Golang environment configuration
├── go.sum
├── config.yaml        # Global configuration
├── Makefile           # Code compilation and other instructions
└── scripts/           # Scripts to perform various operations, keep the root level Makefile small and simple
└── cmd/               # Main application starters of Golang
└── api/               # REST API registry & Static file router, reference: https://github.com/gin-gonic/gin
    ├── api.go
└── deployments/
    └── docker/        # Build docker image contains website and server binary
    └── kubernetes/    # Deployment yaml for k8s
└── commons/           # Common utils for whole project
    └── configs/       # Global configuration reader
    └── database/      # Database connectors
    └── httpclient/    # Http client utils
└── internal/          # Business code & function
    └── entity/        # Object entity
    └── service/       # Service for controller/
    └── controller/    # Deal with http request
    └── dto/           # Output struct(no database)
└── website/           # UI components and pages. detail can jump to  website/README.MD
    ├── yarn.lock      # React environment configuration for machines
    ├── package.json   # React environment configuration for people
    └── src/           # index.js & routes.js/ Components/ Pages/ ...
    └── public/        # HomePage: index.html and icons
```
