import * as React from 'react';
import Container from '@mui/material/Container';
import Accordion from "@mui/material/Accordion";
import AccordionSummary from "@mui/material/AccordionSummary";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import AccordionDetails from "@mui/material/AccordionDetails";
import Box from "@mui/material/Box";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import Layout from './layout/Layout';
import DataGridDemo from './layout/IssueGrid'

const PipelineTabs = () => {
    const [tab, setTab] = React.useState(0);

    const handleChange = (event, newValue) => {
        setTab(newValue);
    };

    const currentVersions = ["TiDB", "TiKV", "TiFlash"];
    return (
        <>
            <Tabs value={tab} onChange={handleChange} aria-label="basic tabs example">
                {/* <Tab label="All" /> */}
                {currentVersions.map((v) => (
                    <Tab label={v}></Tab>
                ))}
            </Tabs>
            <DataGridDemo></DataGridDemo>
        </>
    );
};

const ListPage = () => {
    return (
        <Layout>
            <Container maxWidth="xxl" sx={{mt: 4, mb: 4}}>
                {/*<Paper sx={{p: 2, display: 'flex', flexDirection: 'column'}}>*/}
                {/*</Paper>*/}
                <Accordion defaultExpanded={true}>
                    <AccordionSummary expandIcon={<ExpandMoreIcon/>}>
                        Build Type
                    </AccordionSummary>
                    <AccordionDetails>
                        <Box sx={{width: "100%"}}>
                            <Box sx={{borderBottom: 1, borderColor: "divider"}}></Box>
                            <PipelineTabs></PipelineTabs>
                        </Box>

                    </AccordionDetails>
                </Accordion>
            </Container>
        </Layout>
    )
};

export default ListPage;