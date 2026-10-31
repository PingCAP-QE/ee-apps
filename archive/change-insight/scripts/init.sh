#!/usr/bin/env bash

mkdir workspace && cd workspace

[ ! -d "tidb-engine-ext" ] && git clone https://github.com/pingcap/tidb-engine-ext.git
cd "tidb-engine-ext"
git pull
git branch -r | grep -v '\->' | while read remote; do git branch --track "${remote#origin/}" "$remote"; done
git fetch --all
cd ..
[ ! -d "tidb" ] && git clone https://github.com/pingcap/tidb.git
cd "tidb"
git pull
git branch -r | grep -v '\->' | while read remote; do git branch --track "${remote#origin/}" "$remote"; done
git fetch --all
cd ..
[ ! -d "tiflow" ] && git clone https://github.com/pingcap/tiflow.git
cd "tiflow"
git pull
git branch -r | grep -v '\->' | while read remote; do git branch --track "${remote#origin/}" "$remote"; done
git fetch --all
cd ..
[ ! -d "tiflash" ] && git clone https://github.com/pingcap/tiflash.git
cd "tiflash"
git pull
git branch -r | grep -v '\->' | while read remote; do git branch --track "${remote#origin/}" "$remote"; done
git fetch --all
cd ..
[ ! -d "ng-monitoring" ] && git clone https://github.com/pingcap/ng-monitoring.git
cd "ng-monitoring"
git pull
git branch -r | grep -v '\->' | while read remote; do git branch --track "${remote#origin/}" "$remote"; done
git fetch --all
cd ..
[ ! -d "tikv" ] && git clone https://github.com/tikv/tikv.git
cd "tikv"
git pull
git branch -r | grep -v '\->' | while read remote; do git branch --track "${remote#origin/}" "$remote"; done
git fetch --all
cd ..
[ ! -d "pd" ] && git clone https://github.com/tikv/pd.git
cd "pd"
git pull
git branch -r | grep -v '\->' | while read remote; do git branch --track "${remote#origin/}" "$remote"; done
git fetch --all
cd ..
[ ! -d "migration" ] && git clone https://github.com/tikv/migration.git
cd "migration"
git pull
git branch -r | grep -v '\->' | while read remote; do git branch --track "${remote#origin/}" "$remote"; done
git fetch --all
cd ..
