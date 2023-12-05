import {url} from "../utils"

export function fetchBuildRoll({pipeline_build_id = -1}) {
    return fetch(url(`build/request-rotation?pipeline_build_id=${pipeline_build_id}`))
        .then(async (res) => {
            const data = await res.json();
            let { data: buildResult } = data;
            return buildResult || {};
        })
        .catch((e) => {
            console.log(e);
        });
}

