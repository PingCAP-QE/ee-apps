import {url} from "../utils"

export function fetchBuildResult({pipeline_build_id = -1}) {
    return fetch(url(`build/request-result?pipeline_build_id=${pipeline_build_id}`))
        .then(async (res) => {
            const data = await res.json();
            let { data: buildResult } = data;
            return buildResult || {};
        })
        .catch((e) => {
            console.log(e);
        });
}

