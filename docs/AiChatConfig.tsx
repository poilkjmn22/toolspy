import React from 'react';
import EditForm, { FormItemConfig } from '@/component/EditForm';
interface AiChatConfigProps {
  fontSize?: string | number;
  className?: string;
  isStream?: boolean;
}
const AiChatConfig: React.FC<AiChatConfigProps> = ({ fontSize = 14, isStream = true , className }) => {
  const formConfigs: FormItemConfig[] = [
    {
      label: '字体大小',
      name: 'fontSize',
    },
    {
      label: '流式输出',
      name: 'isStream',
      type: 'switch',
      componentProps: {
        // checkedChildren: '有效',
        // unCheckedChildren: '无效',
      },
    },
  ];
  const handleCancelEdit = () => {};
  const handleSubmitEdit = (record: any, initialValues: any) => {
    if (initialValues?.id) {
    }
    handleCancelEdit();
  };
  return <EditForm className={`${className}`} formConfigs={formConfigs} layout="horizontal" initialValues={{ fontSize, isStream }} />;
};

export default AiChatConfig;
