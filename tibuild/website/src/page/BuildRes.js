import * as React from 'react';
import Container from '@mui/material/Container';
import Accordion from "@mui/material/Accordion";
import AccordionSummary from "@mui/material/AccordionSummary";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import AccordionDetails from "@mui/material/AccordionDetails";
import Box from "@mui/material/Box";
import Layout from '../layout/Layout';
import Grid from '@mui/material/Grid';
import Paper from '@mui/material/Paper';
import ArtifactMeta from "../layout/ArtifactMeta";
import BasicInfo from "../layout/BasicInfo";
import axios from "axios";
import ResponsiveAppBar from "../layout/Header";
import {url} from "../utils";
import Copyright from "../layout/Copyright";

const sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay))

function GetPipelineId() {
    const params = window.location.href;
    const pipeline_build_id = params.split('result/')[1];
    return pipeline_build_id;
}

function FetchDuration(pipeline_id){
    let duration = "40min~60min"
    if (pipeline_id >= 4 && pipeline_id <= 6) {
        duration = "8min~15min"
    } else if (pipeline_id == 7) {
        duration = "1.5h~2h"
    } else if (pipeline_id == 8) {
        duration = "8min~15min"
    } else if (pipeline_id == 9) {
        duration = "1.5h~2h"
    } else if (pipeline_id == 10) {
        duration = "1.5h~2h"
    } else if (pipeline_id == 11) {
        duration = "2h~2.5h"
    } else if (pipeline_id == 12) {
        duration = "5min~15min"
    } else {

    }
    return duration;
}

class BuildResPage extends React.Component {
    constructor(props) {
        super(props);
        let pipeline_build_id = GetPipelineId();
        let duration = "40min~60min"
        this.state = {
            data: {},
            pipeline_build_id: -1,
            duration: duration
        }
        axios.get(url(`build/request-result?pipeline_build_id=${pipeline_build_id}`)).then((response) => {
            let data = response.data.data;
            this.setState({
                data: data,
                pipeline_build_id: pipeline_build_id,
                duration:FetchDuration(data.pipeline_id)
            });
        });

    }


    // 组件渲染后调用
    componentDidMount() {
        let timer = setInterval(function () {
            let data = this.state.data;
            axios.get(url(`build/request-result?pipeline_build_id=${this.state.pipeline_build_id}`)).then((response) => {
                data = response.data.data;
                this.setState({
                    data: data
                });
                console.log(this.state.data.status);
                if (this.state.data.status != "Processing") {
                    clearInterval(timer);
                }

            });

        }.bind(this), 1000*60*5);
    }

    render() {
        return (
            <>
                <ResponsiveAppBar></ResponsiveAppBar>
                <Layout>
                    <Container maxWidth="xxl" sx={{mt: 4, mb: 4}}>
                        <Accordion defaultExpanded={true}>
                            <AccordionSummary expandIcon={<ExpandMoreIcon/>}>
                                <td><font face="Comic Sans MS"> Execution Result</font></td>
                                <td bgcolor="#D1EEEE"><font face="Comic Sans MS"> [{this.state.data.status}]<br/></font></td>

                                <td><font size={2} color={"gray"}>&nbsp;&nbsp;&nbsp;Tips : 1. Predict Duratrion {this.state.duration}. 2. Auto refresh every five minutes.</font>
                                </td>
                            </AccordionSummary>
                            <AccordionDetails>
                                <Box sx={{width: "100%"}}>
                                    <Box sx={{borderBottom: 1, borderColor: "divider"}}></Box>
                                </Box>
                            </AccordionDetails>
                        </Accordion>
                    </Container>
                    <Container maxWidth="xxl" sx={{mt: 4, mb: 4}}>
                        <Grid container spacing={10}>
                            <Grid item xs={7} md={7} lg={7}>
                                <Paper
                                    sx={{
                                        p: 2,
                                        display: 'flex',
                                        flexDirection: 'column',
                                    }}
                                >
                                    <BasicInfo requestBuild={this.state.data}></BasicInfo>
                                </Paper>
                            </Grid>
                            {/* Recent ArtifactMeta */}
                            <Grid item xs={5} md={5} lg={5}>
                                <Paper
                                    sx={{
                                        p: 2,
                                        display: 'flex',
                                        flexDirection: 'column',
                                    }}
                                >
                                    <ArtifactMeta artifactMeta={this.state.data.artifact_meta}></ArtifactMeta>
                                </Paper>
                            </Grid>
                        </Grid>

                    </Container>
                    <Copyright></Copyright>
                </Layout>
            </>
        );
    }
}

export default BuildResPage;
