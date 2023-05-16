package cm

//  Variable management dict Type : FileList, Repo
// Doc by https://pingcap.feishu.cn/docs/doccnvNS9ytJlySxknfHDZx6gLe#2DBzi8

type FileInfo struct {
	RepoUrl  string
	Product  string
	FileList []string
}

func GetConfigValue() map[string][]FileInfo {
	//////////////////////////////////// Variable ////////////////////////////////////
	tiDBVariableManageFile := FileInfo{
		RepoUrl: "pingcap/tidb",
		Product: "TiDB",
		FileList: []string{
			"./sessionctx/variable/",
			"./sessionctx/variable/sysvar.go",
			"./sessionctx/variable/session.go",
			"./sessionctx/variable/tidb_vars.go",
		},
	}
	pDVariableManageFile := FileInfo{
		RepoUrl: "tikv/pd",
		Product: "PD",
		FileList: []string{
			"./server/config/config.go",
			"./server/schedulers/hot_region_config.go",
		},
	}
	//////////////////////////////////// PARSER ////////////////////////////////////
	tiDBParserManageFile := FileInfo{
		RepoUrl: "pingcap/tidb",
		Product: "parser",
		FileList: []string{
			"./parser/parser.y",
		},
	}
	//////////////////////////////////// CONFIG ////////////////////////////////////
	tiDBConfigFile := FileInfo{
		RepoUrl: "pingcap/tidb",
		Product: "TiDB",
		FileList: []string{
			"./config/config.toml.example",
		},
	}
	tiDBBootStrapConfigFile := FileInfo{
		RepoUrl: "pingcap/tidb",
		Product: "BootStrap",
		FileList: []string{
			"./session/bootstrap.go",
		},
	}
	tiDBBRConfigFile := FileInfo{
		RepoUrl: "pingcap/tidb",
		Product: "B & R",
		FileList: []string{
			"./br/tidb-lightning.toml",
			"./cmd/pump/pump.toml",
			"./cmd/drainer/drainer.toml",
		},
	}
	tikvConfigFile := FileInfo{
		RepoUrl: "tikv/tikv",
		Product: "tikv",
		FileList: []string{
			"./etc/config-template.toml",
			"./components/cdc/src/config.rs",
			"./components/batch-system/src/config.rs",
			"./components/pd_client/src/config.rs",
			"./components/sst_importer/src/config.rs",
			"./components/raftstore/src/store/worker/split_config.rs",
			"./components/raftstore/src/coprocessor/config.rs",
			"./components/encryption/src/config.rs",
			"./components/resource_metering/src/config.rs",
			"./src/coprocessor_v2/config.rs",
			"./src/storage/config.rs",
			"./src/server/gc_worker/config.rs",
			"./src/server/lock_manager/config.rs",
			"./src/server/config.rs",
			"./src/config.rs",
		},
	}
	pdConfigFile := FileInfo{
		RepoUrl: "tikv/pd",
		Product: "PD",
		FileList: []string{
			"./conf/config.toml",
			"./metrics/grafana/pd.json",
		},
	}
	dmConfigFile := FileInfo{
		RepoUrl: "pingcap/tiflow",
		Product: "DM",
		FileList: []string{
			"./dm/dm/master/config.go",
			"./dm/dm/worker/config.go",
			"./dm/dm/config/task.go",
			"./dm/dm/config/source_config.go",
		},
	}
	tiCDCConfigFile := FileInfo{
		RepoUrl: "pingcap/tiflow",
		Product: "TiCDC",
		FileList: []string{
			"./pkg/cmd/util/changefeed.toml",
		},
	}

	tiflashConfigFile := FileInfo{
		RepoUrl: "pingcap/tiflash",
		Product: "TiFlash",
		FileList: []string{
			"./dbms/src/Interpreters/Settings.h",
		},
	}

	tiDBEngineExtConfigFile := FileInfo{
		RepoUrl: "pingcap/tidb-engine-ext",
		Product: "tidb-engine-ext",
		FileList: []string{
			"./src/config.rs",
		},
	}

	ngMonitoringConfigFile := FileInfo{
		RepoUrl: "pingcap/ng-monitoring",
		Product: "ng-monitoring",
		FileList: []string{
			"./config/",
		},
	}

	migrationCDCConfigFile := FileInfo{
		RepoUrl: "tikv/migration",
		Product: "tikv/migrationCDC",
		FileList: []string{
			"./cdc/pkg/cmd/util/ticdc.toml",
			"./cdc/pkg/cmd/util/changefeed.toml",
		},
	}

	migrationBRConfigFile := FileInfo{
		RepoUrl: "tikv/migration",
		Product: "tikv/migrationBR",
		FileList: []string{
			"./br/pkg/task/config.go",
			"./br/pkg/task/rawkv_config.go",
			"./br/pkg/task/restore_raw_config.go",
		},
	}

	ConfigManageDict := map[string][]FileInfo{
		"Variable management":   {tiDBVariableManageFile, pDVariableManageFile},
		"Grammar compatibility": {tiDBParserManageFile},
		"Config file": {tiDBConfigFile, tiDBBRConfigFile,
			tikvConfigFile, pdConfigFile, dmConfigFile, tiCDCConfigFile,
			tiflashConfigFile, tiDBEngineExtConfigFile, ngMonitoringConfigFile,
			migrationCDCConfigFile, migrationBRConfigFile},
		"Bootstrap version": {tiDBBootStrapConfigFile},
	}

	return ConfigManageDict
}
