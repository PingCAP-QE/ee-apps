import * as React from 'react';
import {styled} from '@mui/material/styles';
import MuiDrawer from '@mui/material/Drawer';
import Toolbar from '@mui/material/Toolbar';
import List from '@mui/material/List';
import Divider from '@mui/material/Divider';
import IconButton from '@mui/material/IconButton';
import ChevronLeftIcon from '@mui/icons-material/ChevronLeft';
import {fifthListItems, fourthListItems, mainListItems, secondaryListItems, thirdListItems} from './Orders';
import {useNavigate} from "react-router-dom";

const drawerWidth = 240;

const SidebarStyle = styled(MuiDrawer, {shouldForwardProp: (prop) => prop !== 'open'})(
    ({theme, open}) => ({
        '& .MuiDrawer-paper': {
            position: 'relative',
            whiteSpace: 'nowrap',
            width: drawerWidth,
            transition: theme.transitions.create('width', {
                easing: theme.transitions.easing.sharp,
                duration: theme.transitions.duration.enteringScreen,
            }),
            boxSizing: 'border-box',
            ...(!open && {
                overflowX: 'hidden',
                transition: theme.transitions.create('width', {
                    easing: theme.transitions.easing.sharp,
                    duration: theme.transitions.duration.leavingScreen,
                }),
                width: theme.spacing(7),
                [theme.breakpoints.up('sm')]: {
                    width: theme.spacing(9),
                },
            }),
        },
    }),
);

export const Sidebar = (props) => {
    const {open, toggleDrawer} = props;
    const navigate = useNavigate();
    return (
        <>
            <SidebarStyle variant="permanent" open={open}>
                <Toolbar
                    sx={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'flex-end',
                        px: [2],
                    }}
                >
                    {/*<p align={"left"}><font color="#000000" size={6} face="Comic Sans MS">TiBuild</font></p>*/}
                    <IconButton onClick={toggleDrawer}>
                        <ChevronLeftIcon/>
                    </IconButton>
                </Toolbar>
                <Divider/>
                <List>{mainListItems}</List>
                <Divider/>
                <List
                //     onClick={(e) => {
                //     e.preventDefault();
                //     e.stopPropagation();
                //     navigate(`/list/nightly`);
                // }}
                >{secondaryListItems}</List>
                <Divider/>
                <List>{thirdListItems}</List>
                <Divider/>
                <List>{fourthListItems}</List>
                <Divider/>
                <List>{fifthListItems}</List>
                <Divider/>
            </SidebarStyle>
        </>
    );
};
