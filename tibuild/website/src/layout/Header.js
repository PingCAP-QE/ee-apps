import * as React from 'react';
import AppBar from '@mui/material/AppBar';
import Box from '@mui/material/Box';
import Toolbar from '@mui/material/Toolbar';
import Typography from '@mui/material/Typography';
import Button from '@mui/material/Button';
import IconButton from '@mui/material/IconButton';
import { CLIENTID } from "../utils";
import storage from "../request/storageUtils";
import { Badge, Link, Tooltip } from "@mui/material";
import HelpOutlineIcon from '@mui/icons-material/HelpOutline';

function userLoginIn() {
    let url = 'https://github.com/login/oauth/authorize?client_id=' + CLIENTID;
    window.location.href = url //此处的url是全路径
}

export default function ButtonAppBar() {
    const loginName = storage.getUser() === undefined ? 'LOGIN' : storage.getUser();
    const [anchorElNav, setAnchorElNav] = React.useState(null);
    const [anchorElUser, setAnchorElUser] = React.useState(null);

    const handleOpenNavMenu = (event) => {
        setAnchorElNav(event.currentTarget);
    };
    const handleOpenUserMenu = (event) => {
        setAnchorElUser(event.currentTarget);
        if (loginName == 'LOGIN') {
            userLoginIn();
        } else {
            storage.removeUser();
        }
    };

    const handleUserGuide = (event) => {
        const userGuideUrl = "https://pingcap-cn.feishu.cn/wiki/ST9jwAE5ZiPHJUkFjT4cspRSnAf";
        window.open(userGuideUrl);
    }

    const handleCloseNavMenu = () => {
        setAnchorElNav(null);
    };

    const handleCloseUserMenu = () => {
        setAnchorElUser(null);
    };

    return (
        <Box sx={{ flexGrow: 1 }}>
            <AppBar position="static" color="primary">
                <Toolbar>
                    <IconButton
                        size="large"
                        edge="start"
                        color="inherit"
                        aria-label="menu"
                        sx={{ mr: 2 }}
                    >
                        {/*<MenuIcon />*/}
                    </IconButton>
                    <Typography variant="h6" component="div" sx={{ flexGrow: 1 }}>
                        <Link href={"/"} underline="none" align={"left"}><font color="#F5F5F5" size={5} face="Comic Sans MS">TiBuild</font>
                        </Link>
                    </Typography>
                    <Box>
                        <Tooltip title="User Guide">
                            <IconButton color="inherit" onClick={handleUserGuide}>
                                <HelpOutlineIcon />
                            </IconButton>
                        </Tooltip>
                    </Box>
                    <Box sx={{ flexGrow: 0 }}>
                        <Tooltip title="LogIn or LogOut">
                            <Button onClick={handleOpenUserMenu} sx={{ p: 0 }}>
                                {/*<Avatar alt="Remy Sharp" src="/static/images/avatar/2.jpg" />*/}
                                <p align={"left"}><font color="#F5F5F5" size={4} face="Comic Sans MS">{loginName}</font>
                                </p>
                            </Button>
                        </Tooltip>
                    </Box>
                </Toolbar>
            </AppBar>
        </Box>
    );
}
