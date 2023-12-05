import * as React from 'react';
import Container from '@mui/material/Container';
import Accordion from "@mui/material/Accordion";
import AccordionSummary from "@mui/material/AccordionSummary";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import AccordionDetails from "@mui/material/AccordionDetails";
import Box from "@mui/material/Box";
import Layout from './layout/Layout';
import {useNavigate, useParams} from "react-router-dom";
import {fetchPipelines} from "./request/PipelineType";
import {useQuery} from "react-query";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import DataGrid4List from "./layout/GridColumns";
import SendIcon from "@mui/icons-material/Send";
import Button from "@mui/material/Button";
import {FRONTHOST} from './utils'
import ResponsiveAppBar from "./layout/Header";
// import {ReactSession} from 'react-client-session';
import storage from "./request/storageUtils";
import Copyright from "./layout/Copyright";

const ChangeButton = ({pipelineId}) => {
    const navigate = useNavigate();
    return (
        <Button
            variant="contained"
            onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                navigate(`/home/build/config/${pipelineId}`);
            }}
            sx={{mt: 2, ml: 1}}
            endIcon={<SendIcon/>}
            style={{margin: 10}}
            size="small"

        >
            Build
        </Button>
    );
}

const GridList=({tab,currentVersions}) => {
    const [build, setBuild] = React.useState(0);
    const handleBuild = (event, newValue) => {
        setBuild(newValue);
        const pipeline_build_id = currentVersions[tab].pipeline_id;
        if (storage.getUser() == undefined) {
            alert('please log in!');
        } else {
            if (currentVersions[tab].pipeline_id < 10 || (currentVersions[tab].pipeline_id >= 10)) {
                let url = FRONTHOST + '/home/build/config/' + pipeline_build_id;
                window.open(url); //此处的url是全路径
            } else {
                alert('Deny to build! Add auth by group of Ask EE , please!');
            }
        }
    };
    return (
        <>
        <Button
            variant="contained"
            onClick={handleBuild}
            sx={{mt: 2, ml: 1}}
            endIcon={<SendIcon/>}
            style={{margin: 10}}
            size="small"

        >
            Build
        </Button>
    <DataGrid4List tipipelineId={currentVersions[tab].pipeline_id}></DataGrid4List>
        </>
);
}

const PipelineTabs = ({buildTypeProp}) => {
    const [tab, setTab] = React.useState(0);
    const handleChange = (event, newValue) => {
        setTab(newValue)
    };

    const [tabNightly, setTabNightly] = React.useState(0);
    const handleChangeNightly = (event, newValue) => {
        setTabNightly(newValue)
    };

    const buildTypeSelect = buildTypeProp + "-build";
    const pipelineType = useQuery(
        ["build", "pipelines-for-build-type", ...buildTypeSelect],
        () =>
            fetchPipelines({
                build_type: buildTypeSelect
            }),
        {
            onSuccess: (data) => {
                console.log(data)
            },
            keepPreviousData: true,
            staleTime: 5000,
        }
    );
    if (pipelineType.isLoading) {
        return (
            <div>
                <p>Loading...</p>
            </div>
        );
    }
    if (pipelineType.isError) {
        return (
            <div>
                <p>error: {pipelineType.error}</p>
            </div>
        );
    }
    const currentVersions = pipelineType.data;

    if (buildTypeProp == 'dev') {
        return (
            <>
                <Tabs value={tab} onChange={handleChange} aria-label="basic tabs example">
                    {currentVersions.map((v) => (
                        <Tab label={v.pipeline_name}></Tab>
                    ))}
                </Tabs>
                <GridList tab={tab} currentVersions={currentVersions}></GridList>

            </>
        );
    } else if (buildTypeProp == 'nightly') {
        return (
            <>
                <Tabs value={tabNightly} onChange={handleChangeNightly} aria-label="basic tabs example">
                    {currentVersions.map((v) => (
                        <Tab label={v.pipeline_name}></Tab>
                    ))}
                </Tabs>
                <GridList tab={tabNightly} currentVersions={currentVersions}></GridList>
            </>
        );
    } else {
        return (
            <>
                <Tabs value={0} onChange={handleChange} aria-label="basic tabs example">
                    {currentVersions.map((v) => (
                        <Tab label={v.pipeline_name}></Tab>
                    ))}
                </Tabs>
                <GridList tab={0} currentVersions={currentVersions}></GridList>
            </>
        );
    }
}
;

const ListPage = ({props}) => {
    const params = useParams();
    const selectType = params.type === undefined ? "dev" : params.type;

    return (
        <>
            <ResponsiveAppBar></ResponsiveAppBar>
            <Layout>
                <Container maxWidth="xxl" sx={{mt: 4, mb: 4}}>
                    {/*<Paper sx={{p: 2, display: 'flex', flexDirection: 'column'}}>*/}
                    {/*</Paper>*/}
                    <Accordion defaultExpanded={true}>
                        <AccordionSummary expandIcon={<ExpandMoreIcon/>}>
                            <td><font face="Comic Sans MS"> Build Target</font></td>
                        </AccordionSummary>
                        <AccordionDetails>
                            <Box sx={{width: "100%"}}>
                                <Box sx={{borderBottom: 1, borderColor: "divider"}}></Box>
                                <PipelineTabs
                                    buildTypeProp={selectType}
                                ></PipelineTabs>
                            </Box>
                        </AccordionDetails>
                    </Accordion>
                </Container>
                <Copyright></Copyright>
            </Layout>

        </>
    )
};

export default ListPage;
