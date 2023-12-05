import * as React from 'react';
import {useState,useEffect} from 'react';
import {DataGrid, GridColDef, GridToolbar} from '@mui/x-data-grid';
import {useQuery,useQueryClient} from "react-query";
import {fetchList} from "../request/BuildList";
import {FRONTHOST} from "../utils";

const columns: GridColDef[] = [
        {
            field: "id",
            hide: true,
            valueGetter: (params) => params.row.pipeline_build_id,
        },
        {
            field: 'pipeline_build_id',
            headerName: 'ID',
            width: 70,
            valueGetter: (params) => params.row.pipeline_build_id,
            renderCell: function (params) {
                return <p>
                    <a target="_blank"
                       href={`${FRONTHOST}/home/build/result/${params.row.pipeline_build_id}`}>{params.row.pipeline_build_id}</a>
                </p>
            }
        },
        {
            field: 'status',
            headerName: 'Status',
        },
        {
            field: 'begin_time',
            headerName: 'Begin Time',
            width: 170
        },
        {
            field: 'end_time',
            headerName: 'End Time',
            width: 170
        },
        {
            field: 'triggered_by',
            headerName: 'Triggered By',
            width: 120
        },
        {
            field: 'component',
            headerName: 'Component',
            width: 170
        },
        {
            field: 'arch',
            headerName: 'Arch',
            width: 170
        },
        {
            field: 'artifact_type',
            headerName: 'Artifact Type',
            width: 170
        },
        {
            field: 'branch',
            headerName: 'Branch',
            width: 120
        },
        {
            field: 'version',
            headerName: 'Version',
            width: 170
        },
        {
            field: 'push_gcr',
            headerName: 'Push GCR',
        },
        {
            field: 'artifact_meta',
            headerName: 'Artifact Meta',
            renderCell: function (params) {
                return <p>
                    {params.row.artifact_type===""?"not ready":<a target="_blank" href={FRONTHOST+"/home/build/result/"+params.row.pipeline_build_id}>Artifact Meta</a>}
                </p>
            }
        },
        {
            field: 'jenkins_log',
            headerName: 'Jenkins Log',
            width: 100,
            renderCell: function (params) {
                return <p>
                    {params.row.jenkins_log===""?"not ready":<a target="_blank" href={params.row.jenkins_log}>Jenkins Log</a>}
                </p>
            }
        },
    ]
;

const rows = [
    {
        id: 1,
        pipeline_build_id: 1,
        status: 'success',
        begin_time: '2022-06-01 13:00:00',
        end_time: '2022-06-01 14:00:00',
        triggered_by: 'lvhongmeng',
        component: 'TiDB',
        arch: 'linux-amd64',
        artifact_type: 'community image',
        branch: 'release-6.1',
        version: 'v6.1.0',
        artifact_meta: 'binary path',
        jenkins_log: "http://cd.pingcap.net"
    },
    {
        id: 2,
        pipeline_build_id: 2,
        status: 'success',
        begin_time: '2022-06-01 13:00:00',
        end_time: '2022-06-01 14:00:00',
        triggered_by: 'lvhongmeng',
        component: 'TiDB',
        arch: 'linux-amd64',
        artifact_type: 'community image',
        branch: 'release-6.1',
        version: 'v6.1.0',
        artifact_meta: 'binary path',
        jenkins_log: "http://cd.pingcap.net"
    },

];


export default function DataGrid4List(tipipelineId) {
    const id = parseInt(tipipelineId.tipipelineId.toString());
    const [currentPage, setCurrentPage] = useState(1);
    const rowsPerPage = 20;
    // const queryClient = useQueryClient();
    // useEffect(() => {
    //         queryClient.prefetchQuery(
    //             ["build","pipeline-list-show", id,currentPage + 1, ],
    //             () =>
    //                 fetchList({
    //                     pipeline_id: id,
    //                     page: currentPage,
    //                     page_size: rowsPerPage,
    //                 })
    //         );
    // });
    const listQuery = useQuery(
        ["build", "pipeline-list-show", id, currentPage, 1000],
        () =>
            fetchList({
                pipeline_id: id,
                page: currentPage,
                page_size: 1000,
            }),
        {
            onSuccess: (data) => {
                console.log(data)
                // setRowCount(data.length);
            },
            keepPreviousData: true,
            staleTime: 5000,
        }
    );
    if (listQuery.isLoading) {
        return (
            <div>
                <p>Loading...</p>
            </div>
        );
    }
    if (listQuery.isError) {
        return (
            <div>
                <p>error: {listQuery.error}</p>
            </div>
        );
    }
    const r8 = listQuery.data.map((v) => {
            return {...v, id: v.pipeline_build_id}
        }
    )
    return (
        <div style={{height: 400, width: '100%'}}>
            <DataGrid
                rows={r8}
                columns={columns}
                pageSize={rowsPerPage}
                rowsPerPageOptions={[rowsPerPage]}
                onPageChange={(page, details) => {
                    setCurrentPage(page);
                }}
                components={{
                    Toolbar: GridToolbar,
                }}
                disableSelectionOnClick
                showCellRightBorder = {true}
                showColumnRightBorder = {false}

            />
        </div>
    );

}
