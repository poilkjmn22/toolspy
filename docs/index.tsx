// @ts-nocheck
import React, { Component, Fragment } from 'react';
import styles from './index.module.less';
//antd组件
import { antdProxy as antd } from '@/utils/antdProxy';

const { Table, Button, Tag, Popconfirm, message, Drawer, Spin, Modal } = antd;
import { RightOutlined, DownOutlined, FilePdfOutlined } from '@ant-design/icons';
const { CheckableTag } = Tag;
//系统工具
import request, { getNowTime } from 'utils/request';
import modelRequest from 'utils/axiosRequest';
import { SWITCH_ASSET, SET_STATE, LOAD_DYNAMIC, PLAY_ANIM } from 'utils/constant';
//api接口
import {
  queryPageLoops,
  getDeviceInfo,
  delLoop,
  exportLoops,
  getFilterCriteriaV2,
} from '@/api/loop';
//引入组件
import PubSub from 'pubsub-js';
import PaperContext from '@/pages/HomeContent/component/leftSearch/AddLoop/PaperComposition/PaperContext';
import PaperComposition from '@/pages/HomeContent/component/leftSearch/AddLoop/PaperComposition/index.jsx';
import { getStrByParams } from 'utils/dataTransfer';
import SimulationModal from '@/pages/HomeContent/component/leftSearch/Loop/SimulationModal';
import FilePreview from '@/component/FilePreview';

export default class Loop extends Component<{ loopData?: any }> {
  constructor(props) {
    super(props);
    this.state = {
      loading: false,
      loopBtn: [],
      loopData: props.loopData, // 回路数据
      deviceTypeSearchCount: [],
      // 勾选的列表
      checkList: [],
      // ids存储勾选记录中的modelName
      ids: [],
      newBoxs: [],
      cubs: [],
      clickIndex: null,
      total: 0, // 总条数
      currentPage: 1, //分页器pageNo，第几页
      pageSize: 20, //分页器pagesize，一夜多少条数
      // 展开的行
      expKeys: [],
      //
      allexpanded: false,
      // 当前页数据的id列表
      pageList: [],
      selectStatus: 'none',
      // 控制创建窗口中回路/连线的展示
      isLoop: true,
      exportIds: [],
      openDrawerMerge: false,
      paperList: [],
      // 在线仿真
      showSimulationModal: false,
      filePreviewOpen: false,
      currRecordUrl: undefined,
    };

    this.subClickOne = null; // 点击起终点所赋值的索引，用于增加背景颜色
    this.subClickIndex = null; // 点击线芯所赋值的索引，用于增加背景颜色
    this.subClickModelName = null;
  }
  getPaperList = async loopData => {
    try {
      const res = await request({
        url:
          '/ctLoopDoc/ctLoopDoc/list' +
          getStrByParams({
            loopId: loopData?.id,
            column: 'seq',
            order: 'asc',
            pageNo: 1,
            pageSize: 100000,
          }),
        method: 'GET',
      });
      if (res.code === 200) {
        const { records } = res.result;
        this.setState({
          paperList: records || [],
        });
      }
    } catch (error) {
      throw new Error(error);
    }
  };

  processIds = zoneInfo => {
    const { type, id, equipmentSource, modelName } = zoneInfo;
    return {
      areaId: type == '区域' ? id : undefined,
      cubicleId: type === '屏柜' && equipmentSource !== 'PrimaryEquipment' ? id : undefined,
      deviceId: type === '设备' ? id : undefined,
      primaryEquipmentModelName: equipmentSource == 'PrimaryEquipment' ? modelName : undefined,
    };
  };

  /**
   * 点击起点或终点设备跳转
   * @param {*屏柜modelName} cubModelName
   * @param {*行所对应的回路信息} parentRecord
   * @param {*一组线芯数据} records
   * @returns
   */
  jumpCub = async (index, firstRecord, parentRecord, modelName, isEnd = false) => {
    let data = parentRecord;
    let ids = [
      ...new Set(
        parentRecord?.cableCoreInfo?.reduce((acc, item) => {
          acc.push(item?.cableNum, item?.modelName);
          return acc;
        }, []),
      ),
    ];
    data.ids = ids;
    data.active = {
      loop: firstRecord.loopNum,
      link: firstRecord.cableNum,
      cableCoreInfo: firstRecord,
    };
    PubSub.publish('changeInfo', {
      content: { ...data, isEnd },
      dataType: 'loop',
      jumpType: 'cubicle',
    });
    this.subClickOne = index;
    this.subClickModelName = modelName;
    this.setState({}); // 更新state,使react组件重新渲染，确保点击起点、终点后设置选中样式
  };

  // 额外的展开行的渲染结构
  expandedRowRender = parentRecord => {
    const columns = [
      {
        title: '线序',
        dataIndex: 'cpLkSeq',
        key: 'cpLkSeq',
        align: 'center',
        render: (text, record) =>
          record.cableCorePolarity == '正极' ? record.cpLkSeq + '+' : record.cpLkSeq + '−',
      },
      {
        title: '电缆编号',
        dataIndex: 'cableNum',
        key: 'cableNum',
        align: 'center',
        ellipsis: true,
      },
      {
        title: '回路编号',
        dataIndex: 'loopNum',
        key: 'loopNum',
        align: 'center',
        ellipsis: true,
      },
      {
        title: '回路名称',
        dataIndex: 'signalName',
        key: 'signalName',
        align: 'center',
        width: '15rem',
        ellipsis: true,
      },
      {
        title: '首端',
        dataIndex: 'portAName',
        key: 'portAName',
        align: 'center',
        ellipsis: true,
        render: text => text?.slice(text?.indexOf('_') + 1, text?.length)?.replace('_', ':'),
      },
      {
        title: '尾端',
        dataIndex: 'portBName',
        key: 'portBName',
        align: 'center',
        ellipsis: true,
        render: text => text?.slice(text?.indexOf('_') + 1, text?.length)?.replace('_', ':'),
      },
    ];
    // 使用reduce方法按cpLkSeq分组
    const groupedData = parentRecord?.cableCoreInfo?.reduce((groups, record) => {
      const { cpLkSeq } = record;
      if (!groups[cpLkSeq]) {
        groups[cpLkSeq] = [];
      }
      groups[cpLkSeq].push(record);
      return groups;
    }, {});
    // console.log("groupedData :>> ", groupedData);
    // 遍历分组后的数据，对每个组内部进行排序，确保正极在前
    for (const seq in groupedData) {
      groupedData[seq].sort((a, b) => {
        if (a.cableCorePolarity === '正极' && b.cableCorePolarity !== '正极') return -1; // 正极优先
        if (a.cableCorePolarity !== '正极' && b.cableCorePolarity === '正极') return 1; // 负极在正极后
        return 0; // 其他情况保持原顺序
      });
    }
    // console.log("groupedData-------------- :>> ", groupedData);

    // console.log("expandedRowRender", parentRecord);
    return Object.entries(groupedData).map(([seq, records]) => {
      const firstRecord =
        records.find(record => record.loopId + '_' + record.modelName == this.subClickModelName) ||
        records?.[0];
      const originIndex = `${firstRecord?.loopId}_${firstRecord?.cableNum}_0`;
      const terminalIndex = `${firstRecord?.loopId}_${firstRecord?.cableNum}_1`;
      const subClickModelName = `${firstRecord?.loopId}_${firstRecord?.modelName}`;
      return (
        <div key={seq} className="pl-4">
          <div className="my-2">
            <div
              className={`topItem ${originIndex == this.subClickOne ? 'subHighLightRow' : ''}`}
              onClick={() =>
                this.jumpCub(originIndex, firstRecord, parentRecord, subClickModelName)
              }
            >
              <span>起点：</span>
              {firstRecord?.cubicleEquipmentAName}
            </div>
            <div
              className={`topItem ${terminalIndex == this.subClickOne ? 'subHighLightRow' : ''}`}
              onClick={() =>
                this.jumpCub(terminalIndex, firstRecord, parentRecord, subClickModelName, true)
              }
            >
              <span>终点：</span>
              {firstRecord?.cubicleEquipmentBName}
            </div>
          </div>
          <Table
            rowKey={record =>
              'linkInfo_' + record.cableCoreId + '_' + record.loopId + '_' + record.cableNum
            }
            onRow={(record, index) => {
              return {
                onClick: () => {
                  this.rowClickTwo(record, index, parentRecord);
                }, // 点击行
              };
            }}
            size="small"
            bordered
            columns={columns}
            dataSource={records}
            // showHeader={false}
            pagination={false}
            rowClassName={(record, index) =>
              record.loopId + '_' + record.modelName === this.subClickIndex ? 'subHighLightRow' : ''
            }
          />
        </div>
      );
    });
  };

  send2D = async record => {
    PubSub.publish('zoneChange', {
      type: '回路',
      data: {
        ...record,
      },
      to2D: this.props.isTopology,
    });
  };

  //点击 / 勾选后回路的处理
  onSelectChange = (id, records, isClick) => {
    // console.log("onSelectChange", id, records, !notJump);
    let ids = [],
      boxs = [],
      cubs = [],
      // clickIndex,
      chooseData = {}, //勾选的信息框数据
      findObj = {
        isFind: false,
        isLast: false,
        assigned: false,
      },
      data = {
        gotoLoop: '',
        modelName: '',
        space: '',
      },
      exportIds = [];
    const { curentCabinet } = this.$whnzBus;
    for (let i = 0; i < records.length; i++) {
      // loop是勾选记录中的一条
      const loop = records[i];
      exportIds.push(loop.id);
      if (loop.id == id) {
        findObj.isFind = true;
        chooseData = loop;
        // clickIndex = i;
      }
      if (!findObj.isFind && i == records.length - 1) {
        findObj.isLast = true;
        // chooseData = loop;
      }
      for (let j = 0; j < loop.cableCoreInfo.length; j++) {
        // item是勾选记录中的一条回路
        const item = loop.cableCoreInfo[j];
        // ids存储勾选记录中的modelName-回路中连线对应的模型名
        let arrTemp = [...item.modelName.split(','), item.cableNum];
        ids = ids.concat(arrTemp);
        // boxs存储勾选记录中的电缆套管
        let boxTemp = 'Box_' + item.cubicleModelBName + '_' + item.cubicleModelAName;
        boxTemp = boxTemp
          .replace('LQQKZG', '')
          .replace('ZXJCZJG', '')
          .replace('TECKZG', '')
          .replace('HKG', '');
        boxs.push(boxTemp);
        // cubs存储勾选记录中的屏柜
        cubs.push(item.cubicleModelAName);
        cubs.push(item.cubicleModelBName);
        // 找到最后一条记录的最后一条回路信息linkInfo存储
        if (findObj.assigned) {
          continue;
        }
        // 参数id和勾选记录中的一条的id相同，或者该loop是勾选记录中的最后一条，并且该loop中的最后一条回路
        // deviceEntityAName | deviceEntityBName存到data里
        if ((findObj.isFind || findObj.isLast) && j == loop.cableCoreInfo.length - 1) {
          if (
            curentCabinet &&
            loop.cableCoreInfo[j].cubicleModelAName.indexOf(curentCabinet) !== -1
          ) {
            data.gotoLoop =
              loop.cableCoreInfo[j].cubicleModelAName.split(' ')[0] +
              '|' +
              loop.cableCoreInfo[j].deviceEntityAName;
            data.modelName = loop.cableCoreInfo[j].deviceModelAName;
            data.space = loop.cableCoreInfo[j].devASpace;
          } else {
            data.gotoLoop =
              loop.cableCoreInfo[j].cubicleModelBName.split(' ')[0] +
              '|' +
              loop.cableCoreInfo[j].deviceEntityBName;
            data.modelName = loop.cableCoreInfo[j].deviceModelBName;
            data.space = loop.cableCoreInfo[j].devBSpace;
          }
          findObj.assigned = true;
        }
      }
    }
    this.setState({
      exportIds,
    });
    // 未勾选任何记录
    if (JSON.stringify(chooseData) == '{}') {
      this.$whnzBus.sendMessageArr([
        {
          messageType: LOAD_DYNAMIC,
          params: {
            ids: [],
            location: {
              x: 0,
              y: 0,
              z: 0,
            },
            path: '/GFA_HLZ_LingZhou/NengZhongErCi/Mesh/PBR0113/Line/',
            rotation: {
              z: 73,
            },
          },
        },
        {
          messageType: SWITCH_ASSET,
          params: {
            assetId: '',
            mode: 6,
          },
        },
      ]);
      return;
    }
    //对连线号进行去重
    let newIds = [...new Set(ids)];
    let newBoxs = [...new Set(boxs)];
    let newCubs = [...new Set(cubs)];
    this.setState({
      ids: newIds,
      boxs: newBoxs,
      cubs: newCubs,
    });
    chooseData.ids = newIds;
    chooseData.boxs = newBoxs;
    chooseData.cubs = newCubs;
    //判断是否为点击回路事件，否则默认为勾选回路事件
    if (isClick) {
      // 发送信息框信息
      this.$whnzBus.changeInfo = {
        content: chooseData,
        dataType: 'loop',
        jumpType: 'cable',
      };
      PubSub.publish('changeInfo', this.$whnzBus.changeInfo);
    } else {
      // 勾选
      this.$whnzBus.changeInfo = {
        content: chooseData,
        dataType: 'loop',
        jumpType: 'link',
      };
      PubSub.publish('changeInfo', this.$whnzBus.changeInfo);
    }
  };
  //点击行
  rowClick = (row, index) => {
    // console.log("rowClick", row, index);
    let { checkList } = this.state;
    let arrList = [...checkList];
    if (arrList.find(a => a.id === row.id)) {
      arrList;
    } else {
      arrList.push(row);
    }
    this.setState({
      checkList: arrList,
      clickIndex: index,
    });
    this.onSelectChange(row.id, arrList, true);
    this.send2D(row);
    this.subClickOne = null; //选中父级行后，重置子级选中样式
    this.subClickIndex = null; //选中父级行后，重置子级选中样式
    this.subClickModelName = null;
  };
  rowClickTwo = (row, index, parent) => {
    let data = parent,
      ids = [];
    parent.cableCoreInfo.forEach(item => {
      ids = ids.concat([...item.modelName.split(','), item.cableNum]);
    });
    data.ids = [...new Set(ids)];
    data.active = {
      loop: row.num,
      link: row.cableNum,
      cableCoreInfo: row,
    };
    PubSub.publish('changeInfo', {
      content: data,
      dataType: 'loop',
      jumpType: 'loop',
    });
    // console.log("rowClickTwo", data, row, index);
    let { checkList } = this.state;
    let arrList = [...checkList];
    if (!arrList.find(a => a.id === data.id)) {
      arrList.push(data);
      this.setState({
        checkList: arrList,
      });
    }
    this.subClickIndex = row.loopId + '_' + row.modelName;
    this.subClickModelName = row.loopId + '_' + row.modelName;
  };

  rowClassNameFun = (record, index) => {
    const { checkList, clickIndex } = this.state;
    if (index % 2 === 1) {
      if (/* checkList.find(a => a.id === record.id) && */ index == clickIndex) {
        // console.log("record",record);
        return 'mySelfClassName even';
      } else {
        return 'even';
      }
    } else {
      if (/* checkList.find(a => a.id === record.id) && */ index == clickIndex) {
        // console.log("record",record);
        return 'mySelfClassName odd';
      } else {
        return 'odd';
      }
    }
  };
  // 获取勾选状态
  getSelectStatus = () => {
    const { loopData: pageList, checkList } = this.state;
    // console.log("checkList", checkList);
    // console.log("pageList", pageList);

    let status = 'none';

    if (pageList.some(id => checkList.findIndex(item => id.id === item.id) !== -1)) {
      status = 'half';
    }

    if (pageList.every(id => checkList.findIndex(item => id.id === item.id) !== -1)) {
      status = 'all';
    }
    if (checkList.length == 0) {
      status = 'none';
    }
    // console.log("status", status);
    this.setState({
      selectStatus: status,
    });
  };
  handleClickFilePreview = (record: any) => {
    modelRequest({
      url: `/document/getObjectUrl?objectName=${record.fileUrl}`,
      method: 'POST',
    }).then((res: any) => {
      if (res.code == '200') {
        this.setState({ currRecordUrl: res.result, filePreviewOpen: true });
      } else {
        message.error(res.message);
      }
    });
  };
  render() {
    const {
      currentPage,
      pageSize,
      total,
      loopData,
      selectStatus,
      expKeys,
      allexpanded,
      openDrawerMerge,
      paperList,
      loading,
    } = this.state;
    let { layout, points, id: loopId } = this.state.loopData;
    layout && (layout = JSON.parse(layout));
    points && (points = JSON.parse(points));
    return !loopData ? (
      <div className="flex items-center justify-center h-40">暂无回路信息</div>
    ) : (
      <>
        <div className={styles.loopInfo + ' flex flex-col gap-2'}>
          <div className={styles.loopInfoTitle + ' flex items-center gap-2'}>
            回路名称：
            <span className="cursor-pointer" onClick={() => this.rowClick(loopData, 0)}>
              {loopData.name}
            </span>
            <span
              className="cursor-pointer"
              title="查看图纸"
              onClick={async e => {
                await this.getPaperList(loopData);
                this.setState({
                  openDrawerMerge: true,
                });
              }}
            >
              <FilePdfOutlined style={{ fontSize: '1rem' }} />
            </span>
          </div>
          <div className={styles.loopInfoBox}>
            <div>线芯列表：</div>
            {this.expandedRowRender(loopData)}
          </div>
          <div className={styles.loopInfoDocs}>
            <div>相关文件：</div>
            <div className="flex flex-col gap-1 pl-4">
            {loopData.docs?.length > 0 ? loopData.docs.map(doc => {
                return doc.docs?.map(d => (
                  <a
                    onClick={() => this.handleClickFilePreview(d)}
                    href="javascript:;"
                    className="linkAiChat"
                  >
                    {d.documentName}
                  </a>
                ));
            }) : <span className="text-gray-400">暂无文件</span>}
            </div>
          </div>
          <Drawer
            onClose={() => {
              this.setState({
                openDrawerMerge: false,
              });
            }}
            open={openDrawerMerge}
            destroyOnClose
            title="图档关联回路"
            placement="right"
            mask={true}
            styles={{ content: { background: 'none', width: '0' } }}
          >
            <PaperContext.Provider value={{ paperList, layout, points, loopId }}>
              <PaperComposition
                readonly={true}
                onClose={() => {
                  this.setState({
                    openDrawerMerge: false,
                  });
                }}
              ></PaperComposition>
            </PaperContext.Provider>
          </Drawer>
          {this.state.showSimulationModal && (
            <SimulationModal
              onCancel={() => {
                this.setState({ showSimulationModal: false });
              }}
              loopDetail={this.state.loopData}
            />
          )}

          <Modal
            rootClassName="whnz-secondaryCircuit-modal"
            wrapClassName="fileModal"
            open={this.state.filePreviewOpen}
            footer={null}
            onCancel={() => this.setState({ filePreviewOpen: false })}
          >
            <FilePreview showFullScreen={false} style="" file={{ url: this.state.currRecordUrl }} />
          </Modal>
        </div>
      </>
    );
  }
}
