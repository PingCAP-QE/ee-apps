import {url} from "../utils"

export function fetchList({pipeline_id = 1, page = 1, page_size = 100}) {
    return fetch(url(`build/pipeline-list-show?pipeline_id=${pipeline_id}&page=${page}&page_size=${page_size}`))
        .then(async (res) => {
            const data = await res.json();
            let { data: buildList } = data;
            return buildList || [];
        })
        .catch((e) => {
            console.log(e);
        });
}

