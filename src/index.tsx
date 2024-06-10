import {
  definePlugin,
  PanelSection,
  PanelSectionRow,
  ServerAPI,
  staticClasses,
  ToggleField,
} from "decky-frontend-lib";
import { useEffect, useState, VFC } from "react";
import { FaGamepad } from "react-icons/fa";


const Content: VFC<{ serverAPI: ServerAPI }> = ({serverAPI}) => {
  const [scriptEnabled, setScriptEnabled] = useState<boolean>(localStorage.getItem("script_state") == "true");
  const [buttonClicked, setButtonClicked] = useState<boolean>(false);

  useEffect(() => {
    (async () => {
      if (buttonClicked){
        localStorage.setItem("script_state", scriptEnabled ? "true" : "false");
        // console.log(await serverAPI.callPluginMethod('set_script', {'enabled': scriptEnabled}));
        await serverAPI.callPluginMethod('set_script', {'enabled': scriptEnabled});
        setButtonClicked(false);
      }
    })()

  }, [scriptEnabled])


  return (
    <PanelSection title="Options">
      <PanelSectionRow>
        <ToggleField checked={scriptEnabled} label={"Disable deck when docked"} onChange={e => {setScriptEnabled(e); setButtonClicked(true)}}/>
      </PanelSectionRow>
    </PanelSection>
  );
};

export default definePlugin((serverApi: ServerAPI) => {
  return {
    title: <div className={staticClasses.Title}>Steamdeck Input Disabler</div>,
    content: <Content serverAPI={serverApi} />,
    icon: <FaGamepad />
  };
});
