import {url} from "../utils"

export function fetchBuildParam({pipeline_id = -1}) {
    return fetch(url(`build/params-available-for-pipeline?pipeline_id=${pipeline_id}`))
        .then(async (res) => {
            const data = await res.json();
            let { data: buildParamResult } = data;
            return buildParamResult || [];
        })
        .catch((e) => {
            console.log(e);
        });
}

