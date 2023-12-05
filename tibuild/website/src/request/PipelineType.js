import {url} from "../utils"

export function fetchPipelines({build_type=""}) {
    return fetch(url(`build/pipelines-for-build-type?build_type=${build_type}`)).then(async (res) => {
      const data = await res.json();
      let { data: pipelineType } = data;
      return pipelineType || [];
    });
}
